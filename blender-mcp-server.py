"""Local Streamable HTTP MCP server for the Blender Lab TCP bridge.

The MCP server never imports ``bpy``.  Blender Python is sent to the existing
Blender Lab listener as NUL-terminated UTF-8 JSON and is executed in the
currently open interactive Blender instance.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import socket
from pathlib import Path
from typing import Any

import uvicorn
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings


# Security: arbitrary Blender Python is exposed by this process.  Keep both
# listeners on loopback unless every client on the target network is trusted.
DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8765
DEFAULT_BLENDER_HOST = "127.0.0.1"
DEFAULT_BLENDER_PORT = 9876
DEFAULT_SOCKET_TIMEOUT = 30.0
RECV_CHUNK = 64 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024

# The SDK's DNS-rebinding guard tests Origin/Host values by exact match or by a
# literal ``base:`` prefix for ``:*`` patterns.  A browser page served on the
# default HTTP port sends ``Origin: http://127.0.0.1`` with no port, which does
# not match ``http://127.0.0.1:*``, so both the portless and the port-suffixed
# forms are allowed here.  A page on port 80 calling a bridge on 8765 is a
# normal case and should not need extra flags.
DEFAULT_ALLOWED_HOSTS = [
    "127.0.0.1", "localhost", "[::1]",
    "127.0.0.1:*", "localhost:*", "[::1]:*",
]
DEFAULT_ALLOWED_ORIGINS = [
    "http://127.0.0.1", "http://localhost", "http://[::1]",
    "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
]


def _origin_regex(origins: list[str]) -> str:
    """Build a Starlette ``allow_origin_regex`` from Origin allow-list entries.

    Starlette matches this pattern with ``fullmatch`` and has no wildcard-port
    support of its own, so ``:*`` entries become an optional numeric port.  A
    literal ``*`` entry is left to ``allow_origins`` to handle as allow-all.
    """
    parts = []
    for origin in origins:
        if origin == "*":
            continue
        if origin.endswith(":*"):
            parts.append(re.escape(origin[:-2]) + r"(?::\d+)?")
        else:
            parts.append(re.escape(origin))
    return "|".join(f"(?:{part})" for part in parts) if parts else r"(?!)"

# Runtime bridge configuration is updated by main().
BLENDER_HOST = DEFAULT_BLENDER_HOST
BLENDER_PORT = DEFAULT_BLENDER_PORT
SOCKET_TIMEOUT = DEFAULT_SOCKET_TIMEOUT


class BlenderBridgeError(RuntimeError):
    """Base class for concise Blender bridge failures."""


class BlenderConnectionError(BlenderBridgeError):
    """The Blender TCP listener could not be reached or timed out."""


class BlenderProtocolError(BlenderBridgeError):
    """The TCP peer returned an invalid or incomplete bridge response."""


class BlenderExecutionError(BlenderBridgeError):
    """Blender received the request but execution failed."""


def _short(value: Any, limit: int = 800) -> str:
    text = str(value).replace("\x00", "\\0")
    return text if len(text) <= limit else text[:limit] + "..."


def blender_execute(
    code: str,
    strict_json: bool = True,
    host: str | None = None,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Execute Python through the existing NUL-framed Blender Lab bridge.

    Connection/protocol problems and Blender execution problems use distinct
    exception classes so MCP clients receive actionable error messages.
    """
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code must be a non-empty string")

    bridge_host = BLENDER_HOST if host is None else host
    bridge_port = BLENDER_PORT if port is None else port
    socket_timeout = SOCKET_TIMEOUT if timeout is None else timeout
    if not bridge_host:
        raise ValueError("Blender host must not be empty")
    if not 1 <= int(bridge_port) <= 65535:
        raise ValueError("Blender port must be between 1 and 65535")
    if socket_timeout <= 0:
        raise ValueError("Socket timeout must be greater than zero")

    request = {"type": "execute", "code": code, "strict_json": strict_json}
    payload = (
        json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\0"
    ).encode("utf-8")
    response_buffer = bytearray()

    try:
        with socket.create_connection(
            (bridge_host, int(bridge_port)), timeout=socket_timeout
        ) as sock:
            sock.settimeout(socket_timeout)
            sock.sendall(payload)
            while True:
                chunk = sock.recv(RECV_CHUNK)
                if not chunk:
                    raise BlenderProtocolError(
                        "Blender closed the connection before the NUL terminator."
                    )
                response_buffer.extend(chunk)
                terminator = response_buffer.find(b"\0")
                if terminator >= 0:
                    if terminator > MAX_RESPONSE_BYTES:
                        raise BlenderProtocolError(
                            f"Blender response exceeded {MAX_RESPONSE_BYTES} bytes."
                        )
                    response_bytes = bytes(response_buffer[:terminator])
                    break
                if len(response_buffer) > MAX_RESPONSE_BYTES:
                    raise BlenderProtocolError(
                        f"Blender response exceeded {MAX_RESPONSE_BYTES} bytes."
                    )
    except BlenderProtocolError:
        raise
    except (socket.timeout, TimeoutError) as exc:
        raise BlenderConnectionError(
            f"Timed out after {socket_timeout:g}s communicating with Blender "
            f"at {bridge_host}:{bridge_port}."
        ) from exc
    except ConnectionRefusedError as exc:
        raise BlenderConnectionError(
            f"Connection refused by Blender at {bridge_host}:{bridge_port}; "
            "start Blender and the Blender Lab TCP listener."
        ) from exc
    except OSError as exc:
        raise BlenderConnectionError(
            f"Could not connect to Blender at {bridge_host}:{bridge_port}: "
            f"{_short(exc)}"
        ) from exc

    if not response_bytes:
        raise BlenderProtocolError("Blender returned an empty response.")
    try:
        response_text = response_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BlenderProtocolError("Blender returned invalid UTF-8.") from exc
    try:
        response = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise BlenderProtocolError(
            f"Blender returned invalid JSON: {_short(response_text)!r}"
        ) from exc
    if not isinstance(response, dict):
        raise BlenderProtocolError("Blender response must be a JSON object.")

    status = response.get("status")
    if status not in ("ok", "success"):
        detail = response.get("message") or response.get("error") or response
        raise BlenderExecutionError(f"Blender execution failed: {_short(detail)}")
    return response


def _literal(value: Any) -> str:
    """Return a safe Python literal for JSON-shaped MCP arguments."""
    return repr(value)


def _result(code: str) -> Any:
    return blender_execute(code, strict_json=True).get("result")


def _list_result(code: str) -> list[Any]:
    """Unwrap list payloads because Blender Lab strict_json requires a dict root."""
    payload = _result(code)
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise BlenderProtocolError("Blender list response did not contain an items array.")
    return payload["items"]


def _normalize_path(
    filepath: str,
    *,
    must_exist: bool = False,
    output: bool = False,
    suffixes: set[str] | None = None,
) -> str:
    if not filepath or not filepath.strip():
        raise ValueError("filepath must not be empty")
    path = Path(os.path.expandvars(os.path.expanduser(filepath))).resolve()
    if suffixes and path.suffix.lower() not in suffixes:
        allowed = ", ".join(sorted(suffixes))
        raise ValueError(f"Unsupported file extension; expected one of: {allowed}")
    if must_exist and not path.is_file():
        raise ValueError(f"File does not exist: {path}")
    if output and not path.parent.is_dir():
        raise ValueError(f"Destination directory does not exist: {path.parent}")
    return str(path)


def _validate_vector(name: str, value: list[float] | None, length: int) -> None:
    if value is None:
        return
    if len(value) != length or any(not math.isfinite(float(item)) for item in value):
        raise ValueError(f"{name} must contain {length} finite numbers")


BLENDER_HELPERS = r'''
import bpy
import math
import os
from mathutils import Vector

def _require_object(name):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise RuntimeError("Object not found: " + name)
    return obj

def _require_collection(name):
    collection = bpy.data.collections.get(name)
    if collection is None:
        raise RuntimeError("Collection not found: " + name)
    return collection

def _object_mode():
    active = bpy.context.view_layer.objects.active
    if active is not None and active.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')

def _select_only(objects, active=None):
    _object_mode()
    for candidate in bpy.context.selected_objects:
        candidate.select_set(False)
    for candidate in objects:
        candidate.select_set(True)
    bpy.context.view_layer.objects.active = active or (objects[0] if objects else None)

def _object_summary(obj, detailed=False):
    data = {
        "name": obj.name,
        "type": obj.type,
        "location": list(obj.location),
        "rotation_euler": list(obj.rotation_euler),
        "scale": list(obj.scale),
        "dimensions": list(obj.dimensions),
        "visible_viewport": not obj.hide_viewport,
        "visible_render": not obj.hide_render,
        "selected": obj.select_get(),
        "parent": obj.parent.name if obj.parent else None,
        "children": [child.name for child in obj.children],
        "collections": [collection.name for collection in obj.users_collection],
    }
    if detailed:
        data.update({
            "world_location": list(obj.matrix_world.translation),
            "display_type": obj.display_type,
            "mode": obj.mode,
            "materials": [slot.material.name if slot.material else None for slot in obj.material_slots],
            "modifiers": [
                {"name": modifier.name, "type": modifier.type, "show_viewport": modifier.show_viewport,
                 "show_render": modifier.show_render}
                for modifier in obj.modifiers
            ],
            "animation": obj.animation_data is not None,
        })
        if obj.type == 'MESH' and obj.data:
            mesh = obj.data
            data["mesh"] = {
                "name": mesh.name,
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
                "loops": len(mesh.loops),
                "uv_layers": [layer.name for layer in mesh.uv_layers],
                "shape_keys": list(mesh.shape_keys.key_blocks.keys()) if mesh.shape_keys else [],
            }
    return data

def _material_summary(material):
    result = {
        "name": material.name,
        "use_nodes": material.use_nodes,
        "users": material.users,
        "blend_method": getattr(material, "surface_render_method", None),
    }
    if material.use_nodes and material.node_tree:
        node = next((n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if node:
            def value(*names):
                socket = next((node.inputs.get(name) for name in names if node.inputs.get(name)), None)
                if socket is None:
                    return None
                raw = socket.default_value
                return list(raw) if hasattr(raw, '__len__') and not isinstance(raw, str) else raw
            result["principled"] = {
                "base_color": value("Base Color"),
                "metallic": value("Metallic"),
                "roughness": value("Roughness"),
                "alpha": value("Alpha"),
                "emission": value("Emission Color", "Emission"),
                "emission_strength": value("Emission Strength"),
            }
            result["image_textures"] = [
                {"node": n.name, "image": n.image.name if n.image else None,
                 "filepath": bpy.path.abspath(n.image.filepath) if n.image else None}
                for n in material.node_tree.nodes if n.type == 'TEX_IMAGE'
            ]
    return result

def _principled(material):
    material.use_nodes = True
    tree = material.node_tree
    output = next((n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL'), None)
    if output is None:
        output = tree.nodes.new('ShaderNodeOutputMaterial')
    node = next((n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if node is None:
        node = tree.nodes.new('ShaderNodeBsdfPrincipled')
    if not any(link.to_node == output and link.to_socket == output.inputs['Surface'] for link in tree.links):
        tree.links.new(node.outputs['BSDF'], output.inputs['Surface'])
    return node

def _set_socket(node, names, value):
    for name in names:
        socket = node.inputs.get(name)
        if socket is not None:
            socket.default_value = value
            return name
    return None

def _action_fcurves(obj, action):
    if action is None:
        return []
    if getattr(action, 'is_action_layered', False):
        slot = obj.animation_data.action_slot if obj.animation_data else None
        curves = []
        for layer in action.layers:
            for strip in layer.strips:
                if strip.type == 'KEYFRAME':
                    for channelbag in strip.channelbags:
                        if slot is None or channelbag.slot == slot:
                            curves.extend(channelbag.fcurves)
        return curves
    return list(action.fcurves) if hasattr(action, 'fcurves') else []
'''


def _script(body: str) -> str:
    return BLENDER_HELPERS + "\n" + body.strip() + "\n"


mcp = FastMCP("Blender MCP", stateless_http=True, json_response=True)


@mcp.tool()
def ping_blender() -> dict[str, Any]:
    """Verify MCP-to-bridge-to-Blender health; return version, file, scene, and object count."""
    endpoint = f"{BLENDER_HOST}:{BLENDER_PORT}"
    try:
        payload = _result(
            _script(
                """
scene = bpy.context.scene
result = {
    "connected": True,
    "healthy": True,
    "version": bpy.app.version_string,
    "version_tuple": list(bpy.app.version),
    "blend_file": bpy.data.filepath or None,
    "scene_name": scene.name,
    "object_count": len(scene.objects),
}
"""
            )
        )
    except BlenderConnectionError as exc:
        return {
            "connected": False,
            "healthy": False,
            "bridge": endpoint,
            "error_kind": "connection",
            "error": str(exc),
        }
    except BlenderProtocolError as exc:
        return {
            "connected": True,
            "healthy": False,
            "bridge": endpoint,
            "error_kind": "protocol",
            "error": str(exc),
        }
    except BlenderExecutionError as exc:
        return {
            "connected": True,
            "healthy": False,
            "bridge": endpoint,
            "error_kind": "execution",
            "error": str(exc),
        }
    payload["bridge"] = endpoint
    return payload


@mcp.tool()
def get_blender_version() -> dict[str, Any]:
    """Return the connected Blender version and build metadata."""
    return _result(
        _script(
            """
result = {
    "version": bpy.app.version_string,
    "version_tuple": list(bpy.app.version),
    "build_branch": bpy.app.build_branch.decode(errors='replace'),
    "build_platform": bpy.app.build_platform.decode(errors='replace'),
}
"""
        )
    )


@mcp.tool()
def get_scene_info() -> dict[str, Any]:
    """Return a concise scene summary, timeline, selection, camera, and type counts."""
    return _result(
        _script(
            """
scene = bpy.context.scene
counts = {}
for obj in scene.objects:
    counts[obj.type] = counts.get(obj.type, 0) + 1
result = {
    "scene_name": scene.name,
    "blend_file": bpy.data.filepath or None,
    "frame_current": scene.frame_current,
    "frame_start": scene.frame_start,
    "frame_end": scene.frame_end,
    "fps": scene.render.fps / scene.render.fps_base,
    "object_count": len(scene.objects),
    "object_types": counts,
    "active_object": bpy.context.active_object.name if bpy.context.active_object else None,
    "selected_objects": [obj.name for obj in bpy.context.selected_objects],
    "active_camera": scene.camera.name if scene.camera else None,
    "root_collection": scene.collection.name,
}
"""
        )
    )


@mcp.tool()
def list_objects(
    object_type: str = "", include_hidden: bool = True
) -> list[dict[str, Any]]:
    """List scene objects compactly; optionally filter by Blender object type."""
    wanted = object_type.strip().upper()
    return _list_result(
        _script(
            f"""
wanted = {_literal(wanted)}
include_hidden = {_literal(include_hidden)}
result = {{"items": [
    _object_summary(obj)
    for obj in bpy.context.scene.objects
    if (not wanted or obj.type == wanted)
    and (include_hidden or (not obj.hide_viewport and not obj.hide_render))
]}}
"""
        )
    )


@mcp.tool()
def inspect_object(name: str) -> dict[str, Any]:
    """Inspect one object: transforms, hierarchy, collections, materials, modifiers, and mesh counts."""
    return _result(
        _script(f"result = _object_summary(_require_object({_literal(name)}), detailed=True)")
    )


@mcp.tool()
def get_object_hierarchy() -> list[dict[str, Any]]:
    """Return the scene's parent/child hierarchy as compact nested objects."""
    return _list_result(
        _script(
            """
def branch(obj):
    return {"name": obj.name, "type": obj.type, "children": [branch(child) for child in obj.children]}
scene_objects = set(bpy.context.scene.objects)
result = {"items": [branch(obj) for obj in bpy.context.scene.objects if obj.parent not in scene_objects]}
"""
        )
    )


@mcp.tool()
def list_collections() -> list[dict[str, Any]]:
    """List collections with parent, direct objects, children, and visibility flags."""
    return _list_result(
        _script(
            """
parents = {}
for parent in bpy.data.collections:
    for child in parent.children:
        parents.setdefault(child.name, []).append(parent.name)
result = {"items": [{
    "name": collection.name,
    "parents": parents.get(collection.name, []),
    "objects": [obj.name for obj in collection.objects],
    "children": [child.name for child in collection.children],
    "hide_viewport": collection.hide_viewport,
    "hide_render": collection.hide_render,
} for collection in bpy.data.collections]}
"""
        )
    )


@mcp.tool()
def manage_collection(
    action: str, name: str, parent_name: str = ""
) -> dict[str, Any]:
    """Create a collection or delete an empty one. action is create or delete_empty."""
    action = action.strip().lower()
    if action not in {"create", "delete_empty"}:
        raise ValueError("action must be 'create' or 'delete_empty'")
    return _result(
        _script(
            f"""
action = {_literal(action)}
name = {_literal(name)}
parent_name = {_literal(parent_name)}
if action == 'create':
    collection = bpy.data.collections.get(name)
    created = collection is None
    if collection is None:
        collection = bpy.data.collections.new(name)
    parent = _require_collection(parent_name) if parent_name else bpy.context.scene.collection
    if collection.name not in [child.name for child in parent.children]:
        parent.children.link(collection)
    result = {{"name": collection.name, "created": created, "parent": parent.name}}
else:
    collection = _require_collection(name)
    if collection.objects or collection.children:
        raise RuntimeError("Collection is not empty: " + name)
    bpy.data.collections.remove(collection)
    result = {{"name": name, "deleted": True}}
"""
        )
    )


@mcp.tool()
def create_primitive(
    primitive: str,
    name: str = "",
    location_x: float = 0.0,
    location_y: float = 0.0,
    location_z: float = 0.0,
    dimensions: list[float] | None = None,
) -> dict[str, Any]:
    """Create cube, sphere, cylinder, cone, torus, plane, circle, or ico_sphere; units are meters."""
    primitive = primitive.lower().strip()
    operators = {
        "cube": "bpy.ops.mesh.primitive_cube_add",
        "sphere": "bpy.ops.mesh.primitive_uv_sphere_add",
        "uv_sphere": "bpy.ops.mesh.primitive_uv_sphere_add",
        "ico_sphere": "bpy.ops.mesh.primitive_ico_sphere_add",
        "cylinder": "bpy.ops.mesh.primitive_cylinder_add",
        "cone": "bpy.ops.mesh.primitive_cone_add",
        "torus": "bpy.ops.mesh.primitive_torus_add",
        "plane": "bpy.ops.mesh.primitive_plane_add",
        "circle": "bpy.ops.mesh.primitive_circle_add",
    }
    operator = operators.get(primitive)
    if operator is None:
        raise ValueError(f"Unsupported primitive '{primitive}'; supported: {', '.join(operators)}")
    _validate_vector("dimensions", dimensions, 3)
    return _result(
        _script(
            f"""
_object_mode()
{operator}(location=({_literal(location_x)}, {_literal(location_y)}, {_literal(location_z)}))
obj = bpy.context.active_object
requested_name = {_literal(name)}
dimensions = {_literal(dimensions)}
if requested_name:
    obj.name = requested_name
if dimensions is not None:
    obj.dimensions = dimensions
    bpy.context.view_layer.update()
result = _object_summary(obj)
"""
        )
    )


@mcp.tool()
def delete_object(name: str) -> dict[str, Any]:
    """Delete one object by name and unlink it from all collections."""
    return _result(
        _script(
            f"""
name = {_literal(name)}
obj = bpy.data.objects.get(name)
if obj is None:
    result = {{"deleted": False, "name": name, "message": "Object not found"}}
else:
    bpy.data.objects.remove(obj, do_unlink=True)
    result = {{"deleted": True, "name": name}}
"""
        )
    )


@mcp.tool()
def duplicate_object(name: str, new_name: str = "", linked_data: bool = False) -> dict[str, Any]:
    """Duplicate an object; linked_data shares its mesh, while false copies object data."""
    return _result(
        _script(
            f"""
source = _require_object({_literal(name)})
obj = source.copy()
if source.data and not {_literal(linked_data)}:
    obj.data = source.data.copy()
for collection in source.users_collection:
    collection.objects.link(obj)
if {_literal(new_name)}:
    obj.name = {_literal(new_name)}
_select_only([obj])
result = _object_summary(obj, detailed=True)
"""
        )
    )


@mcp.tool()
def rename_object(name: str, new_name: str, rename_data: bool = False) -> dict[str, Any]:
    """Rename an object, optionally renaming its attached data block too."""
    if not new_name.strip():
        raise ValueError("new_name must not be empty")
    return _result(
        _script(
            f"""
obj = _require_object({_literal(name)})
old_name = obj.name
obj.name = {_literal(new_name)}
if {_literal(rename_data)} and obj.data:
    obj.data.name = obj.name
result = {{"old_name": old_name, "name": obj.name, "data_name": obj.data.name if obj.data else None}}
"""
        )
    )


@mcp.tool()
def select_objects(
    names: list[str], mode: str = "replace", active_name: str = ""
) -> dict[str, Any]:
    """Select objects by name. mode is replace, add, or remove; optionally set the active object."""
    mode = mode.strip().lower()
    if mode not in {"replace", "add", "remove"}:
        raise ValueError("mode must be replace, add, or remove")
    return _result(
        _script(
            f"""
names = {_literal(names)}
mode = {_literal(mode)}
objects = [_require_object(name) for name in names]
_object_mode()
if mode == 'replace':
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
for obj in objects:
    obj.select_set(mode != 'remove')
active_name = {_literal(active_name)}
if active_name:
    active = _require_object(active_name)
    active.select_set(True)
    bpy.context.view_layer.objects.active = active
elif mode != 'remove' and objects:
    bpy.context.view_layer.objects.active = objects[0]
result = {{
    "selected": [obj.name for obj in bpy.context.selected_objects],
    "active": bpy.context.active_object.name if bpy.context.active_object else None,
}}
"""
        )
    )


@mcp.tool()
def deselect_all() -> dict[str, Any]:
    """Deselect every object and clear the active object."""
    return _result(
        _script(
            """
_object_mode()
for obj in bpy.context.selected_objects:
    obj.select_set(False)
bpy.context.view_layer.objects.active = None
result = {"selected": [], "active": None}
"""
        )
    )


@mcp.tool()
def set_active_object(name: str, select: bool = True) -> dict[str, Any]:
    """Set the active object by name and optionally ensure it is selected."""
    return _result(
        _script(
            f"""
obj = _require_object({_literal(name)})
_object_mode()
if {_literal(select)}:
    obj.select_set(True)
bpy.context.view_layer.objects.active = obj
result = {{"active": obj.name, "selected": obj.select_get()}}
"""
        )
    )


@mcp.tool()
def set_object_transform(
    name: str,
    location_x: float | None = None,
    location_y: float | None = None,
    location_z: float | None = None,
    rotation_x: float | None = None,
    rotation_y: float | None = None,
    rotation_z: float | None = None,
    scale_x: float | None = None,
    scale_y: float | None = None,
    scale_z: float | None = None,
    dimensions: list[float] | None = None,
) -> dict[str, Any]:
    """Set supplied transform components; rotations are Euler radians and dimensions use scene units."""
    _validate_vector("dimensions", dimensions, 3)
    values = {
        "location_x": location_x, "location_y": location_y, "location_z": location_z,
        "rotation_x": rotation_x, "rotation_y": rotation_y, "rotation_z": rotation_z,
        "scale_x": scale_x, "scale_y": scale_y, "scale_z": scale_z,
    }
    for key, value in values.items():
        if value is not None and not math.isfinite(value):
            raise ValueError(f"{key} must be finite")
    return _result(
        _script(
            f"""
obj = _require_object({_literal(name)})
values = {_literal(values)}
for axis, key in enumerate(('location_x', 'location_y', 'location_z')):
    if values[key] is not None:
        obj.location[axis] = values[key]
for axis, key in enumerate(('rotation_x', 'rotation_y', 'rotation_z')):
    if values[key] is not None:
        obj.rotation_euler[axis] = values[key]
for axis, key in enumerate(('scale_x', 'scale_y', 'scale_z')):
    if values[key] is not None:
        obj.scale[axis] = values[key]
dimensions = {_literal(dimensions)}
if dimensions is not None:
    obj.dimensions = dimensions
bpy.context.view_layer.update()
result = _object_summary(obj)
"""
        )
    )


@mcp.tool()
def apply_object_transforms(
    name: str, location: bool = False, rotation: bool = True, scale: bool = True
) -> dict[str, Any]:
    """Apply selected transforms to one object using deterministic operator context."""
    return _result(
        _script(
            f"""
obj = _require_object({_literal(name)})
_select_only([obj])
outcome = bpy.ops.object.transform_apply(location={_literal(location)}, rotation={_literal(rotation)}, scale={_literal(scale)})
if 'FINISHED' not in outcome:
    raise RuntimeError("Apply transforms was not completed: " + str(outcome))
result = _object_summary(obj)
"""
        )
    )


@mcp.tool()
def set_object_origin(name: str, origin_type: str = "ORIGIN_GEOMETRY", center: str = "MEDIAN") -> dict[str, Any]:
    """Set origin via Blender; origin_type supports ORIGIN_GEOMETRY, ORIGIN_CURSOR, ORIGIN_CENTER_OF_MASS, or GEOMETRY_ORIGIN."""
    allowed = {"ORIGIN_GEOMETRY", "ORIGIN_CURSOR", "ORIGIN_CENTER_OF_MASS", "ORIGIN_CENTER_OF_VOLUME", "GEOMETRY_ORIGIN"}
    origin_type = origin_type.upper().strip()
    center = center.upper().strip()
    if origin_type not in allowed:
        raise ValueError(f"Unsupported origin_type: {origin_type}")
    if center not in {"MEDIAN", "BOUNDS"}:
        raise ValueError("center must be MEDIAN or BOUNDS")
    return _result(
        _script(
            f"""
obj = _require_object({_literal(name)})
_select_only([obj])
outcome = bpy.ops.object.origin_set(type={_literal(origin_type)}, center={_literal(center)})
if 'FINISHED' not in outcome:
    raise RuntimeError("Set origin was not completed: " + str(outcome))
result = _object_summary(obj)
"""
        )
    )


@mcp.tool()
def set_object_parent(name: str, parent_name: str = "", keep_transform: bool = True) -> dict[str, Any]:
    """Parent an object, or unparent it when parent_name is empty; optionally preserve world transform."""
    return _result(
        _script(
            f"""
obj = _require_object({_literal(name)})
world = obj.matrix_world.copy()
parent_name = {_literal(parent_name)}
obj.parent = _require_object(parent_name) if parent_name else None
if {_literal(keep_transform)}:
    obj.matrix_world = world
result = _object_summary(obj)
"""
        )
    )


@mcp.tool()
def join_objects(names: list[str], active_name: str = "") -> dict[str, Any]:
    """Join two or more compatible objects into active_name (or the first name); returns the joined object."""
    if len(names) < 2:
        raise ValueError("At least two object names are required")
    return _result(
        _script(
            f"""
objects = [_require_object(name) for name in {_literal(names)}]
active_name = {_literal(active_name)}
active = _require_object(active_name) if active_name else objects[0]
if active not in objects:
    raise RuntimeError("active_name must be included in names")
_select_only(objects, active)
outcome = bpy.ops.object.join()
if 'FINISHED' not in outcome:
    raise RuntimeError("Join was not completed: " + str(outcome))
result = _object_summary(active, detailed=True)
"""
        )
    )


@mcp.tool()
def move_objects_to_collection(
    names: list[str], collection_name: str, unlink_others: bool = True
) -> dict[str, Any]:
    """Move or additionally link objects to an existing collection."""
    return _result(
        _script(
            f"""
collection = _require_collection({_literal(collection_name)})
objects = [_require_object(name) for name in {_literal(names)}]
for obj in objects:
    if collection.objects.get(obj.name) is None:
        collection.objects.link(obj)
    if {_literal(unlink_others)}:
        for old in list(obj.users_collection):
            if old != collection:
                old.objects.unlink(obj)
result = {{"collection": collection.name, "objects": [obj.name for obj in objects], "unlink_others": {_literal(unlink_others)}}}
"""
        )
    )


@mcp.tool()
def set_object_visibility(
    name: str, viewport_visible: bool | None = None, render_visible: bool | None = None
) -> dict[str, Any]:
    """Show or hide one object independently in the viewport and final render."""
    return _result(
        _script(
            f"""
obj = _require_object({_literal(name)})
viewport_visible = {_literal(viewport_visible)}
render_visible = {_literal(render_visible)}
if viewport_visible is not None:
    obj.hide_viewport = not viewport_visible
if render_visible is not None:
    obj.hide_render = not render_visible
result = {{"name": obj.name, "viewport_visible": not obj.hide_viewport, "render_visible": not obj.hide_render}}
"""
        )
    )


@mcp.tool()
def get_mesh_info(name: str) -> dict[str, Any]:
    """Return mesh counts and basic topology diagnostics without dumping geometry arrays."""
    return _result(
        _script(
            f"""
obj = _require_object({_literal(name)})
if obj.type != 'MESH':
    raise RuntimeError("Object is not a mesh: " + obj.name)
mesh = obj.data
edge_faces = [0] * len(mesh.edges)
for loop in mesh.loops:
    edge_faces[loop.edge_index] += 1
# Each face contributes one loop per edge, so 0 is loose and values other than
# 2 identify boundary/non-manifold edges without mutating mesh data.
non_manifold_edges = sum(1 for count in edge_faces if count != 2)
loose_edges = sum(1 for count in edge_faces if count == 0)
result = {{
    "name": obj.name,
    "vertices": len(mesh.vertices),
    "edges": len(mesh.edges),
    "polygons": len(mesh.polygons),
    "triangles": sum(max(1, len(p.vertices) - 2) for p in mesh.polygons),
    "loose_edges": loose_edges,
    "non_manifold_edges": non_manifold_edges,
    "uv_layers": [layer.name for layer in mesh.uv_layers],
    "material_slots": len(obj.material_slots),
    "has_custom_normals": getattr(mesh, 'has_custom_normals', False),
}}
"""
        )
    )


@mcp.tool()
def clean_mesh(
    name: str,
    merge_distance: float | None = None,
    recalculate_normals: bool = True,
    remove_loose: bool = False,
) -> dict[str, Any]:
    """Safely run edit-mode mesh cleanup; merge_distance uses scene units and None skips merging."""
    if merge_distance is not None and merge_distance < 0:
        raise ValueError("merge_distance must be non-negative")
    return _result(
        _script(
            f"""
obj = _require_object({_literal(name)})
if obj.type != 'MESH':
    raise RuntimeError("Object is not a mesh: " + obj.name)
_select_only([obj])
bpy.ops.object.mode_set(mode='EDIT')
try:
    bpy.ops.mesh.select_all(action='SELECT')
    merge_distance = {_literal(merge_distance)}
    if merge_distance is not None:
        bpy.ops.mesh.remove_doubles(threshold=merge_distance)
    if {_literal(remove_loose)}:
        bpy.ops.mesh.delete_loose(use_verts=True, use_edges=True, use_faces=False)
    if {_literal(recalculate_normals)}:
        bpy.ops.mesh.normals_make_consistent(inside=False)
finally:
    bpy.ops.object.mode_set(mode='OBJECT')
mesh = obj.data
result = {{"name": obj.name, "vertices": len(mesh.vertices), "edges": len(mesh.edges), "polygons": len(mesh.polygons)}}
"""
        )
    )


@mcp.tool()
def add_modifier(
    name: str, modifier_type: str, modifier_name: str = "", settings_json: str = "{}"
) -> dict[str, Any]:
    """Add a common modifier and set simple properties from a JSON object."""
    modifier_type = modifier_type.upper().strip()
    allowed = {"BEVEL", "SOLIDIFY", "SUBSURF", "DECIMATE", "MIRROR", "TRIANGULATE", "ARRAY", "WELD"}
    if modifier_type not in allowed:
        raise ValueError(f"Unsupported modifier type; supported: {', '.join(sorted(allowed))}")
    try:
        settings = json.loads(settings_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"settings_json is invalid JSON: {exc.msg}") from exc
    if not isinstance(settings, dict) or any(not isinstance(key, str) for key in settings):
        raise ValueError("settings_json must contain a JSON object")
    return _result(
        _script(
            f"""
obj = _require_object({_literal(name)})
modifier_type = {_literal(modifier_type)}
modifier = obj.modifiers.new(name={_literal(modifier_name)} or modifier_type.title(), type=modifier_type)
settings = {_literal(settings)}
for key, value in settings.items():
    if key.startswith('_') or not hasattr(modifier, key):
        raise RuntimeError("Unsupported modifier property: " + key)
    try:
        setattr(modifier, key, value)
    except Exception as exc:
        raise RuntimeError("Could not set modifier property " + key + ": " + str(exc))
result = {{"object": obj.name, "modifier": modifier.name, "type": modifier.type}}
"""
        )
    )


@mcp.tool()
def apply_modifier(name: str, modifier_name: str) -> dict[str, Any]:
    """Apply one named modifier to a mesh using an explicit active-object context."""
    return _result(
        _script(
            f"""
obj = _require_object({_literal(name)})
if obj.modifiers.get({_literal(modifier_name)}) is None:
    raise RuntimeError("Modifier not found: " + {_literal(modifier_name)})
_select_only([obj])
outcome = bpy.ops.object.modifier_apply(modifier={_literal(modifier_name)})
if 'FINISHED' not in outcome:
    raise RuntimeError("Apply modifier was not completed: " + str(outcome))
result = _object_summary(obj, detailed=True)
"""
        )
    )


@mcp.tool()
def set_shading(name: str, smooth: bool = True, angle_radians: float | None = None) -> dict[str, Any]:
    """Set all mesh faces smooth or flat; optional smoothing angle is in radians."""
    if angle_radians is not None and not 0 <= angle_radians <= math.pi:
        raise ValueError("angle_radians must be between 0 and pi")
    return _result(
        _script(
            f"""
obj = _require_object({_literal(name)})
if obj.type != 'MESH':
    raise RuntimeError("Object is not a mesh: " + obj.name)
for polygon in obj.data.polygons:
    polygon.use_smooth = {_literal(smooth)}
angle = {_literal(angle_radians)}
if angle is not None and hasattr(obj.data, 'set_sharp_from_angle'):
    obj.data.set_sharp_from_angle(angle=angle)
result = {{"name": obj.name, "smooth": {_literal(smooth)}, "polygons": len(obj.data.polygons), "angle_radians": angle}}
"""
        )
    )


@mcp.tool()
def list_materials() -> list[dict[str, Any]]:
    """List materials with concise Principled values, texture paths, and user counts."""
    return _list_result(_script("result = {'items': [_material_summary(material) for material in bpy.data.materials]}"))


@mcp.tool()
def inspect_material_assignments(name: str) -> dict[str, Any]:
    """Return an object's material slots and mesh polygon assignment counts."""
    return _result(
        _script(
            f"""
obj = _require_object({_literal(name)})
counts = {{}}
if obj.type == 'MESH':
    for polygon in obj.data.polygons:
        counts[str(polygon.material_index)] = counts.get(str(polygon.material_index), 0) + 1
result = {{
    "object": obj.name,
    "active_material_index": obj.active_material_index,
    "slots": [{{"index": i, "material": slot.material.name if slot.material else None}} for i, slot in enumerate(obj.material_slots)],
    "polygon_counts_by_slot": counts,
}}
"""
        )
    )


@mcp.tool()
def create_principled_material(
    name: str,
    reuse: bool = True,
    base_color: list[float] | None = None,
    metallic: float | None = None,
    roughness: float | None = None,
    alpha: float | None = None,
    emission_color: list[float] | None = None,
    emission_strength: float | None = None,
) -> dict[str, Any]:
    """Create/reuse a node material and set common PBR values; colors are linear RGBA arrays."""
    _validate_vector("base_color", base_color, 4)
    _validate_vector("emission_color", emission_color, 4)
    for label, value in (("metallic", metallic), ("roughness", roughness), ("alpha", alpha)):
        if value is not None and not 0 <= value <= 1:
            raise ValueError(f"{label} must be between 0 and 1")
    if emission_strength is not None and emission_strength < 0:
        raise ValueError("emission_strength must be non-negative")
    values = {
        "base_color": base_color, "metallic": metallic, "roughness": roughness,
        "alpha": alpha, "emission_color": emission_color, "emission_strength": emission_strength,
    }
    return _result(
        _script(
            f"""
name = {_literal(name)}
material = bpy.data.materials.get(name) if {_literal(reuse)} else None
created = material is None
if material is None:
    material = bpy.data.materials.new(name=name)
node = _principled(material)
values = {_literal(values)}
if values['base_color'] is not None: _set_socket(node, ('Base Color',), values['base_color'])
if values['metallic'] is not None: _set_socket(node, ('Metallic',), values['metallic'])
if values['roughness'] is not None: _set_socket(node, ('Roughness',), values['roughness'])
if values['alpha'] is not None:
    _set_socket(node, ('Alpha',), values['alpha'])
    material.diffuse_color[3] = values['alpha']
if values['emission_color'] is not None: _set_socket(node, ('Emission Color', 'Emission'), values['emission_color'])
if values['emission_strength'] is not None: _set_socket(node, ('Emission Strength',), values['emission_strength'])
result = _material_summary(material)
result['created'] = created
"""
        )
    )


@mcp.tool()
def assign_material(object_name: str, material_name: str, replace_slots: bool = False) -> dict[str, Any]:
    """Assign an existing material, optionally replacing all current slots."""
    return _result(
        _script(
            f"""
obj = _require_object({_literal(object_name)})
material = bpy.data.materials.get({_literal(material_name)})
if material is None:
    raise RuntimeError("Material not found: " + {_literal(material_name)})
if obj.data is None or not hasattr(obj.data, 'materials'):
    raise RuntimeError("Object data does not support materials: " + obj.name)
if {_literal(replace_slots)}:
    obj.data.materials.clear()
if material.name not in [item.name for item in obj.data.materials if item]:
    obj.data.materials.append(material)
result = {{"object": obj.name, "materials": [item.name if item else None for item in obj.data.materials]}}
"""
        )
    )


@mcp.tool()
def remove_material(object_name: str, material_name: str = "", slot_index: int | None = None) -> dict[str, Any]:
    """Remove one object material slot by index or material name; leaves the material data block intact."""
    if slot_index is not None and slot_index < 0:
        raise ValueError("slot_index must be non-negative")
    return _result(
        _script(
            f"""
obj = _require_object({_literal(object_name)})
if obj.data is None or not hasattr(obj.data, 'materials'):
    raise RuntimeError("Object data does not support materials: " + obj.name)
materials = obj.data.materials
index = {_literal(slot_index)}
material_name = {_literal(material_name)}
if index is None:
    index = next((i for i, material in enumerate(materials) if material and material.name == material_name), None)
if index is None or index >= len(materials):
    raise RuntimeError("Material slot not found")
removed = materials[index].name if materials[index] else None
materials.pop(index=index)
result = {{"object": obj.name, "removed": removed, "materials": [item.name if item else None for item in materials]}}
"""
        )
    )


@mcp.tool()
def set_material_image_texture(
    material_name: str,
    filepath: str,
    target: str = "base_color",
    colorspace: str = "sRGB",
) -> dict[str, Any]:
    """Load a local image and connect it to base_color, roughness, metallic, alpha, normal, or emission."""
    path = _normalize_path(filepath, must_exist=True)
    target = target.lower().strip()
    allowed = {"base_color", "roughness", "metallic", "alpha", "normal", "emission"}
    if target not in allowed:
        raise ValueError(f"target must be one of: {', '.join(sorted(allowed))}")
    return _result(
        _script(
            f"""
material = bpy.data.materials.get({_literal(material_name)})
if material is None:
    raise RuntimeError("Material not found: " + {_literal(material_name)})
node = _principled(material)
tree = material.node_tree
path = {_literal(path)}
image = bpy.data.images.load(path, check_existing=True)
try:
    image.colorspace_settings.name = {_literal(colorspace)}
except Exception as exc:
    raise RuntimeError("Unsupported image colorspace: " + str(exc))
texture = tree.nodes.new('ShaderNodeTexImage')
texture.name = "MCP_" + {_literal(target)}
texture.label = os.path.basename(path)
texture.image = image
target = {_literal(target)}
socket_names = {{'base_color': ('Base Color',), 'roughness': ('Roughness',), 'metallic': ('Metallic',),
                'alpha': ('Alpha',), 'emission': ('Emission Color', 'Emission')}}
if target == 'normal':
    normal = tree.nodes.new('ShaderNodeNormalMap')
    tree.links.new(texture.outputs['Color'], normal.inputs['Color'])
    tree.links.new(normal.outputs['Normal'], node.inputs['Normal'])
else:
    destination = next((node.inputs.get(name) for name in socket_names[target] if node.inputs.get(name)), None)
    if destination is None:
        raise RuntimeError("Principled input is unavailable for target: " + target)
    output = texture.outputs['Alpha'] if target == 'alpha' else texture.outputs['Color']
    tree.links.new(output, destination)
result = {{"material": material.name, "image": image.name, "filepath": bpy.path.abspath(image.filepath), "target": target, "node": texture.name}}
"""
        )
    )


@mcp.tool()
def import_asset(filepath: str) -> dict[str, Any]:
    """Import a local GLB/glTF, FBX, OBJ, or STL and return newly created object names."""
    path = _normalize_path(filepath, must_exist=True, suffixes={".glb", ".gltf", ".fbx", ".obj", ".stl"})
    suffix = Path(path).suffix.lower()
    return _result(
        _script(
            f"""
path = {_literal(path)}
suffix = {_literal(suffix)}
before = set(bpy.data.objects)
if suffix in ('.glb', '.gltf'):
    outcome = bpy.ops.import_scene.gltf(filepath=path)
elif suffix == '.fbx':
    if not hasattr(bpy.ops.import_scene, 'fbx'):
        raise RuntimeError("FBX import operator is unavailable in this Blender installation")
    outcome = bpy.ops.import_scene.fbx(filepath=path)
elif suffix == '.obj':
    if hasattr(bpy.ops.wm, 'obj_import'):
        outcome = bpy.ops.wm.obj_import(filepath=path)
    elif hasattr(bpy.ops.import_scene, 'obj'):
        outcome = bpy.ops.import_scene.obj(filepath=path)
    else:
        raise RuntimeError("OBJ import operator is unavailable in this Blender installation")
else:
    if hasattr(bpy.ops.wm, 'stl_import'):
        outcome = bpy.ops.wm.stl_import(filepath=path)
    elif hasattr(bpy.ops.import_mesh, 'stl'):
        outcome = bpy.ops.import_mesh.stl(filepath=path)
    else:
        raise RuntimeError("STL import operator is unavailable in this Blender installation")
if 'FINISHED' not in outcome:
    raise RuntimeError("Import was not completed: " + str(outcome))
imported = [obj for obj in bpy.data.objects if obj not in before]
result = {{"imported": True, "filepath": path, "objects": [obj.name for obj in imported], "object_count": len(imported)}}
"""
        )
    )


@mcp.tool()
def export_gltf(filepath: str, selected_only: bool = False) -> dict[str, Any]:
    """Export GLB or glTF to an explicit path; .glb is binary and selected_only limits export."""
    path = _normalize_path(filepath, output=True, suffixes={".glb", ".gltf"})
    export_format = "GLB" if Path(path).suffix.lower() == ".glb" else "GLTF_SEPARATE"
    return _result(
        _script(
            f"""
path = {_literal(path)}
selected_only = {_literal(selected_only)}
if selected_only and not bpy.context.selected_objects:
    raise RuntimeError("selected_only was requested but no objects are selected")
outcome = bpy.ops.export_scene.gltf(filepath=path, export_format={_literal(export_format)}, use_selection=selected_only)
if 'FINISHED' not in outcome:
    raise RuntimeError("glTF export was not completed: " + str(outcome))
result = {{"exported": True, "filepath": path, "format": {_literal(export_format)}, "selected_only": selected_only,
          "objects": [obj.name for obj in bpy.context.selected_objects] if selected_only else None}}
"""
        )
    )


@mcp.tool()
def save_blend_file(filepath: str = "") -> dict[str, Any]:
    """Save the current .blend, or save-as only to the explicitly supplied destination."""
    path = _normalize_path(filepath, output=True, suffixes={".blend"}) if filepath else ""
    return _result(
        _script(
            f"""
path = {_literal(path)}
if path:
    outcome = bpy.ops.wm.save_as_mainfile(filepath=path)
else:
    if not bpy.data.filepath:
        raise RuntimeError("The current Blender file has no filepath; provide an explicit .blend filepath")
    outcome = bpy.ops.wm.save_mainfile()
if 'FINISHED' not in outcome:
    raise RuntimeError("Save was not completed: " + str(outcome))
result = {{"saved": True, "filepath": bpy.data.filepath}}
"""
        )
    )


@mcp.tool()
def create_or_update_light(
    name: str,
    light_type: str = "POINT",
    energy: float = 1000.0,
    color: list[float] | None = None,
    location: list[float] | None = None,
    rotation: list[float] | None = None,
) -> dict[str, Any]:
    """Create or update a POINT, SUN, SPOT, or AREA light; rotation is Euler radians."""
    light_type = light_type.upper().strip()
    if light_type not in {"POINT", "SUN", "SPOT", "AREA"}:
        raise ValueError("light_type must be POINT, SUN, SPOT, or AREA")
    if energy < 0:
        raise ValueError("energy must be non-negative")
    _validate_vector("color", color, 3)
    _validate_vector("location", location, 3)
    _validate_vector("rotation", rotation, 3)
    return _result(
        _script(
            f"""
name = {_literal(name)}
obj = bpy.data.objects.get(name)
created = obj is None
if obj is None:
    data = bpy.data.lights.new(name=name, type={_literal(light_type)})
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
elif obj.type != 'LIGHT':
    raise RuntimeError("Existing object is not a light: " + name)
obj.data.type = {_literal(light_type)}
obj.data.energy = {_literal(energy)}
color = {_literal(color)}
location = {_literal(location)}
rotation = {_literal(rotation)}
if color is not None: obj.data.color = color
if location is not None: obj.location = location
if rotation is not None: obj.rotation_euler = rotation
result = {{"name": obj.name, "created": created, "type": obj.data.type, "energy": obj.data.energy,
          "color": list(obj.data.color), "location": list(obj.location), "rotation_euler": list(obj.rotation_euler)}}
"""
        )
    )


@mcp.tool()
def create_or_update_camera(
    name: str,
    location: list[float] | None = None,
    rotation: list[float] | None = None,
    lens_mm: float | None = None,
    clip_start: float | None = None,
    clip_end: float | None = None,
    set_active: bool = False,
) -> dict[str, Any]:
    """Create/update a perspective camera; rotation is Euler radians and distances use scene units."""
    _validate_vector("location", location, 3)
    _validate_vector("rotation", rotation, 3)
    if lens_mm is not None and lens_mm <= 0:
        raise ValueError("lens_mm must be positive")
    return _result(
        _script(
            f"""
name = {_literal(name)}
obj = bpy.data.objects.get(name)
created = obj is None
if obj is None:
    data = bpy.data.cameras.new(name=name)
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
elif obj.type != 'CAMERA':
    raise RuntimeError("Existing object is not a camera: " + name)
values = {{"location": {_literal(location)}, "rotation": {_literal(rotation)}, "lens": {_literal(lens_mm)},
          "clip_start": {_literal(clip_start)}, "clip_end": {_literal(clip_end)}}}
if values['location'] is not None: obj.location = values['location']
if values['rotation'] is not None: obj.rotation_euler = values['rotation']
if values['lens'] is not None: obj.data.lens = values['lens']
if values['clip_start'] is not None: obj.data.clip_start = values['clip_start']
if values['clip_end'] is not None: obj.data.clip_end = values['clip_end']
if {_literal(set_active)}: bpy.context.scene.camera = obj
result = {{"name": obj.name, "created": created, "active": bpy.context.scene.camera == obj,
          "location": list(obj.location), "rotation_euler": list(obj.rotation_euler), "lens_mm": obj.data.lens,
          "clip_start": obj.data.clip_start, "clip_end": obj.data.clip_end}}
"""
        )
    )


@mcp.tool()
def aim_camera(camera_name: str, target_object: str = "", target_point: list[float] | None = None) -> dict[str, Any]:
    """Aim a camera's -Z axis at an object origin or XYZ point using deterministic quaternion math."""
    _validate_vector("target_point", target_point, 3)
    if not target_object and target_point is None:
        raise ValueError("Provide target_object or target_point")
    return _result(
        _script(
            f"""
camera = _require_object({_literal(camera_name)})
if camera.type != 'CAMERA':
    raise RuntimeError("Object is not a camera: " + camera.name)
target_object = {_literal(target_object)}
target = _require_object(target_object).matrix_world.translation if target_object else Vector({_literal(target_point)})
direction = target - camera.matrix_world.translation
if direction.length == 0:
    raise RuntimeError("Camera and target occupy the same point")
camera.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
result = {{"camera": camera.name, "target": list(target), "rotation_euler": list(camera.rotation_euler)}}
"""
        )
    )


@mcp.tool()
def get_render_settings() -> dict[str, Any]:
    """Return common render engine, resolution, output, image format, and sampling settings."""
    return _result(
        _script(
            """
scene = bpy.context.scene
render = scene.render
engine_items = render.bl_rna.properties['engine'].enum_items
result = {
    "engine": render.engine,
    "available_engines": [item.identifier for item in engine_items],
    "resolution_x": render.resolution_x,
    "resolution_y": render.resolution_y,
    "resolution_percentage": render.resolution_percentage,
    "fps": render.fps / render.fps_base,
    "output_filepath": bpy.path.abspath(render.filepath) if render.filepath else "",
    "file_format": render.image_settings.file_format,
    "film_transparent": render.film_transparent,
    "samples": getattr(scene.cycles, 'samples', None) if hasattr(scene, 'cycles') else None,
}
"""
        )
    )


@mcp.tool()
def set_render_settings(
    resolution_x: int | None = None,
    resolution_y: int | None = None,
    resolution_percentage: int | None = None,
    engine: str = "",
    output_filepath: str = "",
    file_format: str = "",
    film_transparent: bool | None = None,
    samples: int | None = None,
) -> dict[str, Any]:
    """Set common render options; engine/format must be available in the connected Blender build."""
    for label, value in (("resolution_x", resolution_x), ("resolution_y", resolution_y), ("samples", samples)):
        if value is not None and value <= 0:
            raise ValueError(f"{label} must be positive")
    if resolution_percentage is not None and not 1 <= resolution_percentage <= 100:
        raise ValueError("resolution_percentage must be between 1 and 100")
    output = _normalize_path(output_filepath, output=True) if output_filepath else ""
    values = {
        "x": resolution_x, "y": resolution_y, "percentage": resolution_percentage,
        "engine": engine.strip(), "output": output, "format": file_format.strip().upper(),
        "transparent": film_transparent, "samples": samples,
    }
    return _result(
        _script(
            f"""
scene = bpy.context.scene
render = scene.render
values = {_literal(values)}
if values['x'] is not None: render.resolution_x = values['x']
if values['y'] is not None: render.resolution_y = values['y']
if values['percentage'] is not None: render.resolution_percentage = values['percentage']
if values['engine']:
    try: render.engine = values['engine']
    except Exception as exc: raise RuntimeError("Unsupported render engine: " + str(exc))
if values['output']: render.filepath = values['output']
if values['format']:
    try: render.image_settings.file_format = values['format']
    except Exception as exc: raise RuntimeError("Unsupported render format: " + str(exc))
if values['transparent'] is not None: render.film_transparent = values['transparent']
if values['samples'] is not None:
    if not hasattr(scene, 'cycles'): raise RuntimeError("Cycles settings are unavailable")
    scene.cycles.samples = values['samples']
result = {{"engine": render.engine, "resolution_x": render.resolution_x, "resolution_y": render.resolution_y,
          "resolution_percentage": render.resolution_percentage, "output_filepath": bpy.path.abspath(render.filepath) if render.filepath else '',
          "file_format": render.image_settings.file_format, "film_transparent": render.film_transparent,
          "samples": getattr(scene.cycles, 'samples', None) if hasattr(scene, 'cycles') else None}}
"""
        )
    )


@mcp.tool()
def render_still(filepath: str = "", write_still: bool = True) -> dict[str, Any]:
    """Render the current frame; an explicit filepath overrides the scene output for this render."""
    path = _normalize_path(filepath, output=True) if filepath else ""
    return _result(
        _script(
            f"""
scene = bpy.context.scene
path = {_literal(path)}
if path: scene.render.filepath = path
if {_literal(write_still)} and not scene.render.filepath:
    raise RuntimeError("write_still requires a render output filepath")
outcome = bpy.ops.render.render(write_still={_literal(write_still)})
if 'FINISHED' not in outcome:
    raise RuntimeError("Render was not completed: " + str(outcome))
result = {{"rendered": True, "frame": scene.frame_current, "filepath": bpy.path.abspath(scene.render.filepath) if scene.render.filepath else None,
          "write_still": {_literal(write_still)}}}
"""
        )
    )


@mcp.tool()
def set_timeline(
    current_frame: int | None = None, start_frame: int | None = None, end_frame: int | None = None
) -> dict[str, Any]:
    """Set the current frame and/or inclusive scene frame range."""
    if start_frame is not None and end_frame is not None and start_frame > end_frame:
        raise ValueError("start_frame must not exceed end_frame")
    return _result(
        _script(
            f"""
scene = bpy.context.scene
start = {_literal(start_frame)}
end = {_literal(end_frame)}
current = {_literal(current_frame)}
if start is not None: scene.frame_start = start
if end is not None: scene.frame_end = end
if scene.frame_start > scene.frame_end: raise RuntimeError("Resulting frame range is invalid")
if current is not None: scene.frame_set(current)
result = {{"current_frame": scene.frame_current, "start_frame": scene.frame_start, "end_frame": scene.frame_end}}
"""
        )
    )


@mcp.tool()
def keyframe_transform(
    name: str, frame: int, properties: list[str] | None = None, interpolation: str = "BEZIER"
) -> dict[str, Any]:
    """Insert location/rotation_euler/scale keyframes at a frame; interpolation is CONSTANT, LINEAR, or BEZIER."""
    props = properties or ["location", "rotation_euler", "scale"]
    if not props or any(prop not in {"location", "rotation_euler", "scale"} for prop in props):
        raise ValueError("properties may contain location, rotation_euler, and scale")
    interpolation = interpolation.upper().strip()
    if interpolation not in {"CONSTANT", "LINEAR", "BEZIER"}:
        raise ValueError("interpolation must be CONSTANT, LINEAR, or BEZIER")
    return _result(
        _script(
            f"""
obj = _require_object({_literal(name)})
frame = {_literal(frame)}
properties = {_literal(props)}
for prop in properties:
    if not obj.keyframe_insert(data_path=prop, frame=frame):
        raise RuntimeError("Could not insert keyframe for " + prop)
action = obj.animation_data.action if obj.animation_data else None
if action:
    for fcurve in _action_fcurves(obj, action):
        for point in fcurve.keyframe_points:
            if round(point.co.x) == frame:
                point.interpolation = {_literal(interpolation)}
result = {{"object": obj.name, "frame": frame, "properties": properties, "interpolation": {_literal(interpolation)}}}
"""
        )
    )


@mcp.tool()
def delete_transform_keyframes(
    name: str, frame: int, properties: list[str] | None = None
) -> dict[str, Any]:
    """Delete common transform keyframes for one object at the specified frame."""
    props = properties or ["location", "rotation_euler", "scale"]
    if any(prop not in {"location", "rotation_euler", "scale"} for prop in props):
        raise ValueError("properties may contain location, rotation_euler, and scale")
    return _result(
        _script(
            f"""
obj = _require_object({_literal(name)})
deleted = []
for prop in {_literal(props)}:
    if obj.keyframe_delete(data_path=prop, frame={_literal(frame)}): deleted.append(prop)
result = {{"object": obj.name, "frame": {_literal(frame)}, "deleted": deleted}}
"""
        )
    )


@mcp.tool()
def get_animation_info(name: str = "") -> dict[str, Any]:
    """Return concise action, frame range, curve paths, and keyframe counts for one object or all animated objects."""
    return _result(
        _script(
            f"""
objects = [_require_object({_literal(name)})] if {_literal(name)} else list(bpy.context.scene.objects)
animated = []
for obj in objects:
    animation = obj.animation_data
    action = animation.action if animation else None
    if action:
        curves = _action_fcurves(obj, action)
        animated.append({{
            "object": obj.name,
            "action": action.name,
            "frame_range": list(action.frame_range),
            "fcurves": [{{"data_path": curve.data_path, "array_index": curve.array_index,
                         "keyframes": len(curve.keyframe_points)}} for curve in curves],
        }})
result = {{"current_frame": bpy.context.scene.frame_current, "animated_objects": animated}}
"""
        )
    )


@mcp.tool()
def center_object(name: str, axes: str = "XYZ") -> dict[str, Any]:
    """Move an object's world-space bounding-box center to zero on selected X/Y/Z axes."""
    axes = axes.upper().strip()
    if not axes or any(axis not in "XYZ" for axis in axes):
        raise ValueError("axes must contain X, Y, and/or Z")
    return _result(
        _script(
            f"""
obj = _require_object({_literal(name)})
corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
center = sum(corners, Vector()) / 8.0
for axis, letter in enumerate('XYZ'):
    if letter in {_literal(axes)}: obj.location[axis] -= center[axis]
bpy.context.view_layer.update()
result = _object_summary(obj)
"""
        )
    )


@mcp.tool()
def place_object_on_ground(name: str, ground_z: float = 0.0) -> dict[str, Any]:
    """Translate an object so its world-space bounding-box minimum Z equals ground_z."""
    return _result(
        _script(
            f"""
obj = _require_object({_literal(name)})
minimum = min((obj.matrix_world @ Vector(corner)).z for corner in obj.bound_box)
obj.location.z += {_literal(ground_z)} - minimum
bpy.context.view_layer.update()
result = _object_summary(obj)
"""
        )
    )


@mcp.tool()
def prepare_for_gltf(
    names: list[str] | None = None,
    apply_rotation: bool = True,
    apply_scale: bool = True,
    triangulate: bool = False,
    shade_smooth: bool = False,
) -> dict[str, Any]:
    """Explicitly prepare named or selected meshes for GLB: apply transforms and optional triangulation/smoothing."""
    return _result(
        _script(
            f"""
names = {_literal(names)}
objects = [_require_object(name) for name in names] if names is not None else list(bpy.context.selected_objects)
if not objects: raise RuntimeError("No objects supplied or selected")
non_mesh = [obj.name for obj in objects if obj.type != 'MESH']
if non_mesh: raise RuntimeError("Only mesh objects can be prepared: " + ', '.join(non_mesh))
processed = []
for obj in objects:
    _select_only([obj])
    outcome = bpy.ops.object.transform_apply(location=False, rotation={_literal(apply_rotation)}, scale={_literal(apply_scale)})
    if 'FINISHED' not in outcome: raise RuntimeError("Could not apply transforms to " + obj.name)
    if {_literal(triangulate)}:
        modifier = obj.modifiers.new(name='MCP_Triangulate', type='TRIANGULATE')
        outcome = bpy.ops.object.modifier_apply(modifier=modifier.name)
        if 'FINISHED' not in outcome: raise RuntimeError("Could not triangulate " + obj.name)
    if {_literal(shade_smooth)}:
        for polygon in obj.data.polygons: polygon.use_smooth = True
    processed.append(_object_summary(obj, detailed=True))
_select_only(objects, objects[0])
result = {{"prepared": [obj['name'] for obj in processed], "objects": processed}}
"""
        )
    )


@mcp.tool()
def validate_game_assets(
    names: list[str] | None = None, polygon_warning_threshold: int = 100000
) -> dict[str, Any]:
    """Diagnose named or selected game assets without changing them; reports scale, materials, visibility, and polygon risks."""
    if polygon_warning_threshold <= 0:
        raise ValueError("polygon_warning_threshold must be positive")
    return _result(
        _script(
            f"""
names = {_literal(names)}
objects = [_require_object(name) for name in names] if names is not None else list(bpy.context.selected_objects)
if not objects: raise RuntimeError("No objects supplied or selected")
reports = []
for obj in objects:
    issues = []
    if not obj.name.strip(): issues.append({{"code": "empty_name", "severity": "error"}})
    if obj.type != 'MESH': issues.append({{"code": "non_mesh", "severity": "warning", "type": obj.type}})
    if any(abs(value - 1.0) > 1e-5 for value in obj.scale): issues.append({{"code": "unapplied_scale", "severity": "warning", "scale": list(obj.scale)}})
    if any(value < 0 for value in obj.scale): issues.append({{"code": "negative_scale", "severity": "warning", "scale": list(obj.scale)}})
    if obj.hide_viewport or obj.hide_render: issues.append({{"code": "hidden", "severity": "warning", "viewport": obj.hide_viewport, "render": obj.hide_render}})
    if obj.type == 'MESH':
        polygons = len(obj.data.polygons)
        triangles = sum(max(1, len(poly.vertices) - 2) for poly in obj.data.polygons)
        if not obj.material_slots or all(slot.material is None for slot in obj.material_slots): issues.append({{"code": "missing_material", "severity": "warning"}})
        if polygons > {_literal(polygon_warning_threshold)}: issues.append({{"code": "high_polygon_count", "severity": "warning", "polygons": polygons}})
        if not obj.data.uv_layers: issues.append({{"code": "missing_uv", "severity": "info"}})
    else:
        polygons = None
        triangles = None
    reports.append({{"name": obj.name, "type": obj.type, "polygons": polygons, "triangles": triangles, "issues": issues, "valid": not any(i['severity'] == 'error' for i in issues)}})
result = {{"object_count": len(reports), "issue_count": sum(len(item['issues']) for item in reports), "objects": reports}}
"""
        )
    )


@mcp.tool()
def execute_blender_python(code: str) -> Any:
    """Execute arbitrary multiline Python in Blender. Assign the desired JSON-serializable return value to ``result``.

    This unrestricted fallback exposes the complete Blender Python API. Prefer
    structured tools for routine work, and keep the MCP server local/trusted.
    """
    # Blender Lab strict_json requires the root result to be a dictionary.  A
    # tiny suffix preserves the public fallback's ability to return any nested
    # JSON value, including a list, scalar, or null.
    wrapped_code = (
        code.rstrip()
        + "\n_blender_mcp_user_result = result\n"
        + "result = {'value': _blender_mcp_user_result}\n"
    )
    payload = blender_execute(wrapped_code, strict_json=True).get("result")
    if not isinstance(payload, dict) or "value" not in payload:
        raise BlenderProtocolError("Blender fallback response did not contain a value field.")
    return payload["value"]


def _positive_port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _positive_timeout(value: str) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive finite number")
    return timeout


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MCP server bridge for the Blender Lab TCP listener.")
    parser.add_argument(
        "--mcp-host", "--host", dest="mcp_host", default=DEFAULT_MCP_HOST,
        help="MCP Streamable HTTP host (default: 127.0.0.1; --host is a legacy alias).",
    )
    parser.add_argument(
        "--mcp-port", "--port", dest="mcp_port", default=DEFAULT_MCP_PORT, type=_positive_port,
        help="MCP Streamable HTTP port (default: 8765; --port is a legacy alias).",
    )
    parser.add_argument("--blender-host", default=DEFAULT_BLENDER_HOST, help="Blender Lab TCP bridge host.")
    parser.add_argument("--blender-port", default=DEFAULT_BLENDER_PORT, type=_positive_port, help="Blender Lab TCP bridge port.")
    parser.add_argument("--socket-timeout", default=DEFAULT_SOCKET_TIMEOUT, type=_positive_timeout, help="Bridge connect/read timeout in seconds.")
    parser.add_argument(
        "--allow-origin", action="append", default=[], metavar="ORIGIN",
        help="Extra browser Origin allowed to call /mcp (repeatable), e.g. https://example.com. "
             "Append ':*' to accept any port, e.g. https://example.com:*.",
    )
    parser.add_argument(
        "--allow-host", action="append", default=[], metavar="HOST",
        help="Extra Host header value accepted by the DNS-rebinding guard (repeatable), e.g. example.com.",
    )
    parser.add_argument(
        "--web-root", default="", metavar="DIR",
        help="Directory holding index.html to serve alongside /mcp (default: the server script's folder).",
    )
    parser.add_argument(
        "--no-web", action="store_true",
        help="Serve only the MCP endpoint and do not publish the tester page.",
    )
    return parser


class MCPHTTPFramingMiddleware:
    """Let the HTTP server frame MCP responses, including empty acknowledgments.

    A local HTTP interceptor was observed replacing Content-Length with
    Transfer-Encoding: chunked without encoding the body. Omitting the length
    lets Uvicorn generate actual chunk framing, which passes through intact.
    Keep JSON bodies untouched; transfer coding belongs to the HTTP server.
    """

    def __init__(self, app: ASGIApp, path: str) -> None:
        self.app = app
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != self.path:
            await self.app(scope, receive, send)
            return

        async def send_without_length(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [
                        (name, value) for name, value in message.get("headers", [])
                        if name.lower() != b"content-length"
                    ],
                }
            await send(message)

        await self.app(scope, receive, send_without_length)


def _build_app(
    web_root: Path, allowed_origins: list[str], publish_page: bool
) -> tuple[Any, Path]:
    """Assemble the ASGI app: the MCP endpoint plus an optional tester page.

    FastMCP emits no CORS headers, so a page served from a different origin (for
    example a static server on port 80 calling this bridge on 8765) can send the
    request but cannot read the response.  Serving the tester page from this same
    process keeps the browser on one origin, so CORS never applies at all; the
    middleware stays for the case where the page is hosted elsewhere.
    """
    app = mcp.streamable_http_app()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_origin_regex=_origin_regex(allowed_origins),
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type", "Accept", "MCP-Protocol-Version",
            "Mcp-Session-Id", "Last-Event-ID",
        ],
        expose_headers=["Mcp-Session-Id"],
        max_age=600,
    )
    app.add_middleware(MCPHTTPFramingMiddleware, path=mcp.settings.streamable_http_path)

    # Only index.html and the assets folder are published; the server source and
    # the rest of the directory stay private.
    index_path = web_root / "index.html"

    def serve_index(request: Any) -> Response:
        if not index_path.is_file():
            return Response(f"index.html not found in {web_root}", status_code=404)
        return FileResponse(index_path, media_type="text/html")

    def serve_favicon(request: Any) -> Response:
        icon = web_root / "favicon.ico"
        if icon.is_file():
            return FileResponse(icon, media_type="image/x-icon")
        return Response(status_code=204)

    if publish_page:
        # Appended after the /mcp route, so the MCP endpoint keeps precedence.
        app.router.routes.append(Route("/", serve_index, methods=["GET"]))
        app.router.routes.append(Route("/index.html", serve_index, methods=["GET"]))
        app.router.routes.append(Route("/favicon.ico", serve_favicon, methods=["GET"]))
        assets_dir = web_root / "assets"
        if assets_dir.is_dir():
            app.router.routes.append(Mount("/assets", app=StaticFiles(directory=str(assets_dir))))

    return app, index_path


def main() -> None:
    global BLENDER_HOST, BLENDER_PORT, SOCKET_TIMEOUT

    args = _build_parser().parse_args()

    BLENDER_HOST = args.blender_host
    BLENDER_PORT = args.blender_port
    SOCKET_TIMEOUT = args.socket_timeout
    mcp.settings.host = args.mcp_host
    mcp.settings.port = args.mcp_port

    allowed_hosts = list(dict.fromkeys(DEFAULT_ALLOWED_HOSTS + args.allow_host))
    allowed_origins = list(dict.fromkeys(DEFAULT_ALLOWED_ORIGINS + args.allow_origin))
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )

    base_url = f"http://{args.mcp_host}:{args.mcp_port}"
    print(f"Blender MCP endpoint: {base_url}/mcp")
    print(f"Blender Lab bridge: {BLENDER_HOST}:{BLENDER_PORT} (timeout {SOCKET_TIMEOUT:g}s)")
    if args.allow_origin or args.allow_host:
        print(f"Extra CORS origins: {', '.join(args.allow_origin) or '(none)'}")
        print(f"Extra allowed hosts: {', '.join(args.allow_host) or '(none)'}")
    if args.mcp_host not in {"127.0.0.1", "localhost", "::1"}:
        print("WARNING: arbitrary Blender Python is exposed on a non-loopback MCP host.")

    web_root = Path(args.web_root).expanduser().resolve() if args.web_root else Path(__file__).resolve().parent
    app, index_path = _build_app(web_root, allowed_origins, publish_page=not args.no_web)

    if args.no_web:
        print("Tester page:      disabled (--no-web)")
    elif index_path.is_file():
        print(f"Tester page:      {base_url}/")
    else:
        print(f"Tester page:      index.html not found in {web_root} (serving /mcp only)")

    uvicorn.run(app, host=args.mcp_host, port=args.mcp_port, log_level="info")


if __name__ == "__main__":
    main()

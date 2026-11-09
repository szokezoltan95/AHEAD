"""Compile optional measured floor-plan geometry and drawn water routes."""

from collections import defaultdict, deque
from dataclasses import dataclass
from math import dist, isfinite, log, pi
from re import fullmatch

from ahead.simulator.presets import PRESETS


WALL_TYPES = {
    "insulated_brick": (("brick", 0.25), ("eps", 0.15)),
    "insulated_masonry_350": (("finish_coat", 0.002), ("adhesive", 0.003), ("certificate_eps", 0.15),
                              ("adhesive", 0.003), ("lime_plaster", 0.015),
                              ("solid_masonry", 0.30), ("lime_plaster", 0.015)),
    "insulated_masonry_300": (("finish_coat", 0.002), ("adhesive", 0.003), ("certificate_eps", 0.15),
                              ("adhesive", 0.003), ("lime_plaster", 0.015),
                              ("solid_masonry", 0.30), ("lime_plaster", 0.015)),
    "insulated_concrete_footing": (("concrete", 0.45), ("xps", 0.10)),
    "solid_brick": (("brick", 0.25),),
    "insulated_block": (("block", 0.25), ("mineral_wool", 0.15)),
    "partition": (("brick", 0.12),),
    "insulated_ceiling": (("rockwool", 0.20), ("perlite_concrete", 0.05),
                          ("reinforced_concrete", 0.25), ("lime_plaster", 0.015)),
    "insulated_ceiling_22": (("rockwool", 0.20), ("perlite_concrete", 0.05),
                             ("reinforced_concrete", 0.22), ("lime_plaster", 0.015)),
    "insulated_floor": (("concrete", 0.15), ("xps", 0.10)),
    "basement_ceiling": (("laminate", 0.01), ("adhesive", 0.003), ("floor_screed", 0.05),
                         ("floor_insulation", 0.03), ("reinforced_concrete", 0.25),
                         ("lime_plaster", 0.015)),
    "masonry_unheated": (("lime_plaster", 0.015), ("solid_masonry", 0.30), ("lime_plaster", 0.015)),
}

CONSTRUCTION_U = {
    "insulated_masonry_350": 0.262, "insulated_masonry_300": 0.262,
    "insulated_concrete_footing": 0.29, "insulated_ceiling": 0.186,
    "insulated_ceiling_22": 0.187, "basement_ceiling": 0.855,
    "masonry_unheated": 0.810,
}

PLAN_SIDES = {"north": 0, "east": 90, "south": 180, "west": 270}
OPPOSITE = {"north": "south", "east": "west", "south": "north", "west": "east"}
MOUNTING = ("on_wall", "in_wall", "underfloor", "service_void", "free_running")
TOLERANCE = 1e-6


def _number(value, name, lower, upper):
    if type(value) not in (float, int) or not isfinite(value) or not lower <= value <= upper:
        raise ValueError(f"{name} must be between {lower} and {upper}")
    return value


def _entries(plan, section):
    entries = plan.get(section, {})
    if not isinstance(entries, dict) or len(entries) > 500:
        raise ValueError(f"Floor plan {section} must be an object with at most 500 entries")
    for key, item in entries.items():
        if not fullmatch(r"[a-zA-Z0-9_-]{1,64}", key) or not isinstance(item, dict):
            raise ValueError(f"Invalid floor plan {section} entry: {key}")
    return entries


def validate_plan(plan):
    if not isinstance(plan, dict) or plan.keys() - {"rooms", "nodes", "pipes"}:
        raise ValueError("Floor plan contains unsupported fields")
    for section in ("rooms", "nodes", "pipes"):
        _entries(plan, section)


def _side(rect, side):
    x, y, width, height = rect
    if side in ("north", "south"):
        return y if side == "north" else y + height, x, x + width
    return x if side == "west" else x + width, y, y + height


def _same(a, b):
    return abs(a - b) < TOLERANCE


def _floorplan_rooms(plan, rows):
    layouts = _entries(plan, "rooms")
    rectangles = {}
    for name, layout in layouts.items():
        if name not in rows["rooms"]:
            raise ValueError(f"Floor-plan room {name} is missing from the room list")
        room = rows["rooms"][name]
        if type(layout.get("derive", False)) is not bool:
            raise ValueError(f"Room {name} has an invalid derive setting")
        x = _number(layout.get("x"), f"{name} x position", -500, 500)
        y = _number(layout.get("y"), f"{name} y position", -500, 500)
        width = _number(room["length_m"], f"{name} length", 0.25, 100)
        height = _number(room["width_m"], f"{name} width", 0.25, 100)
        rectangles[name] = (x, y, width, height)
    for index, (name, (x, y, width, height)) in enumerate(rectangles.items()):
        for other, (ox, oy, ow, oh) in list(rectangles.items())[:index]:
            if rows["rooms"][name]["level"] != rows["rooms"][other]["level"]:
                continue
            if min(x + width, ox + ow) - max(x, ox) > TOLERANCE and min(y + height, oy + oh) - max(y, oy) > TOLERANCE:
                if layouts[name].get("derive") or layouts[other].get("derive"):
                    raise ValueError(f"Floor-plan rooms {name} and {other} overlap")
    return layouts, rectangles


def _surface(rows, name, preset, material, **values):
    if name in rows["surfaces"]:
        raise ValueError(f"Generated floor-plan surface conflicts with {name}")
    rows["surfaces"][name] = PRESETS["surfaces"][preset] | values | {
        "u_override": (0.629 if material == "insulated_floor" and values.get("boundary") == "ground"
                       else CONSTRUCTION_U.get(material, 0.0)), "preset": preset,
    }
    for index, (layer, thickness) in enumerate(WALL_TYPES[material]):
        rows["layers"][f"{name}_{index}"] = PRESETS["layers"][layer] | {
            "preset": layer, "surface": name, "thickness_m": thickness,
        }


def _vertical_extent(room, levels):
    level = levels[room["level"]]
    floor = level["elevation_m"] + _number(room["floor_offset_m"], "Room floor offset", -10, 10)
    return floor, floor + (room["height_m"] or level["height_m"])


def derive_envelope(plan, rows):
    layouts, rectangles = _floorplan_rooms(plan, rows)
    derived = {name for name, layout in layouts.items() if layout.get("derive")}
    old_walls = {
        name for name, row in rows["surfaces"].items()
        if row["preset"] == "wall" and (row["room"] in derived or row["adjacent_room"] in derived)
    }
    for name in old_walls:
        del rows["surfaces"][name]
    for collection in ("layers", "windows"):
        rows[collection] = {name: row for name, row in rows[collection].items()
                            if row["surface"] not in old_walls}
    for name in sorted(derived):
        layout = layouts[name]
        room = rows["rooms"][name]
        floor, ceiling = _vertical_extent(room, rows["levels"])
        room_height = ceiling - floor
        walls = layout.get("walls", {})
        if not isinstance(walls, dict) or walls.keys() - PLAN_SIDES.keys():
            raise ValueError(f"Invalid wall settings for {name}")
        for side, azimuth in PLAN_SIDES.items():
            wall = walls.get(side, {})
            if not isinstance(wall, dict):
                raise ValueError(f"Invalid {side} wall in {name}")
            anchor, start, end = _side(rectangles[name], side)
            neighbours = []
            for other, rectangle in rectangles.items():
                if other == name or rows["rooms"][other]["level"] != room["level"]:
                    continue
                fixed, first, last = _side(rectangle, OPPOSITE[side])
                first, last = max(start, first), min(end, last)
                if _same(anchor, fixed) and last - first > TOLERANCE:
                    neighbours.append((first, last, other))
            cursor = start
            spans = []
            for first, last, other in sorted(neighbours):
                if first - cursor > TOLERANCE:
                    spans.append((cursor, first, None))
                spans.append((first, last, other))
                cursor = last
            if end - cursor > TOLERANCE:
                spans.append((cursor, end, None))
            windows = wall.get("openings", [])
            if not isinstance(windows, list) or len(windows) > 50:
                raise ValueError(f"Invalid openings on {name}'s {side} wall")
            for index, (first, last, other) in enumerate(spans):
                surface = f"plan_{name}_{side}_{index + 1}"
                construction = wall.get("construction", "insulated_brick")
                if construction not in WALL_TYPES:
                    raise ValueError(f"Unknown wall construction for {name}")
                burial = _number(wall.get("ground_height_m", 0), "Buried wall height", 0, room_height)
                width = last - first
                overlap = 0.0
                if other:
                    other_floor, other_ceiling = _vertical_extent(rows["rooms"][other], rows["levels"])
                    overlap = max(0, min(ceiling, other_ceiling) - max(floor, other_floor))
                    if overlap > TOLERANCE and not (other in derived and other < name):
                        unheated = (rows["rooms"][name]["preset"] == "unheated" or
                                    rows["rooms"][other]["preset"] == "unheated")
                        _surface(rows, surface, "wall", "masonry_unheated" if unheated else "partition",
                                 room=name, adjacent_room=other,
                                 boundary="room", area_m2=width * overlap, azimuth_deg=azimuth)
                outside = room_height - overlap
                buried = min(burial, outside)
                if buried > TOLERANCE:
                    _surface(rows, surface + "_ground", "wall", construction, room=name,
                             boundary="ground", area_m2=width * buried, azimuth_deg=azimuth)
                if outside - buried > TOLERANCE:
                    _surface(rows, surface + "_exposed" if other else surface, "wall", construction,
                             room=name, area_m2=width * (outside - buried), azimuth_deg=azimuth)
            occupied = []
            for index, opening in enumerate(windows):
                if not isinstance(opening, dict):
                    raise ValueError(f"Invalid opening in {name}")
                preset = opening.get("preset", "double_glazed")
                if preset not in PRESETS["windows"]:
                    raise ValueError(f"Unknown opening type in {name}")
                width = _number(opening.get("width", 1.2), "Opening width", 0.1, 25)
                height = _number(opening.get("height", 1.1), "Opening height", 0.1, room_height)
                distance = _number(opening.get("distance", (end - start) / 2),
                                   "Opening position", 0, end - start)
                left = start + distance - width / 2
                right = left + width
                if any(left < b - TOLERANCE and right > a + TOLERANCE for a, b in occupied):
                    raise ValueError(f"Openings overlap on {name}'s {side} wall")
                occupied.append((left, right))
                for span, (first, last, other) in enumerate(spans):
                    surface = f"plan_{name}_{side}_{span + 1}"
                    if (not other and surface in rows["surfaces"]
                            and first <= left + TOLERANCE and right <= last + TOLERANCE
                            and height <= room_height - wall.get("ground_height_m", 0) + TOLERANCE):
                        key = f"plan_{name}_{side}_opening_{index + 1}"
                        rows["windows"][key] = PRESETS["windows"][preset] | {
                            "preset": preset, "surface": f"plan_{name}_{side}_{span + 1}",
                            "area_m2": width * height,
                        }
                        break
                else:
                    raise ValueError(f"An opening must fit on an exterior portion of {name}'s {side} wall")
    _horizontal_surfaces(derived, rectangles, rows)
    return rectangles


def _horizontal_surfaces(derived, rectangles, rows):
    for name in sorted(derived):
        room = rows["rooms"][name]
        floor, ceiling = _vertical_extent(room, rows["levels"])
        x, y, width, height = rectangles[name]
        for preset, elevation in (("floor", floor), ("ceiling", ceiling)):
            if any(row["room"] == name and row["preset"] in (
                {"floor"} if preset == "floor" else {"ceiling", "roof"}
            ) for row in rows["surfaces"].values()):
                continue
            uncovered = width * height
            for other, (ox, oy, ow, oh) in rectangles.items():
                if other == name:
                    continue
                adjacent = rows["rooms"][other]
                other_floor, other_ceiling = _vertical_extent(adjacent, rows["levels"])
                if not _same(elevation, other_ceiling if preset == "floor" else other_floor):
                    continue
                area = max(0, min(x + width, ox + ow) - max(x, ox)) * max(0, min(y + height, oy + oh) - max(y, oy))
                if area <= TOLERANCE:
                    continue
                uncovered -= area
                reverse = any(
                    row["preset"] == ("ceiling" if preset == "floor" else "floor")
                    and row["room"] == other and row["adjacent_room"] == name
                    for row in rows["surfaces"].values()
                )
                if not reverse:
                    construction = ("basement_ceiling" if preset == "floor" and
                                    adjacent["preset"] == "unheated" else "insulated_floor")
                    _surface(rows, f"plan_{name}_{preset}_{other}", preset, construction,
                             room=name, adjacent_room=other, boundary="room", area_m2=area)
            if uncovered > TOLERANCE:
                boundary = "ground" if preset == "floor" and elevation <= TOLERANCE else "outdoors"
                construction = ("insulated_floor" if preset == "floor" else
                               layouts[name].get("ceiling_construction", "insulated_ceiling"))
                if construction not in WALL_TYPES or (preset == "ceiling" and construction not in
                                                       {"insulated_ceiling", "insulated_ceiling_22"}):
                    raise ValueError(f"Unknown {preset} construction for {name}")
                _surface(rows, f"plan_{name}_{preset}", preset, construction,
                         room=name, boundary=boundary, area_m2=uncovered)


@dataclass(frozen=True)
class Pipe:
    name: str
    circuit: str
    run: str
    first: str
    last: str
    length_m: float
    kw_k: float
    room: str | None
    mounting: str
    points: tuple
    diameter_mm: float
    insulation_mm: float
    offset_mm: float
    wall: str
    wall_thickness_mm: float
    material: str
    water_volume_l: float
    metal_mass_kg: float


def compile_pipes(plan, rows, rectangles):
    pipes = {}
    nodes = _entries(plan, "nodes")
    for name, node in nodes.items():
        level = node.get("level")
        if level not in rows["levels"]:
            raise ValueError(f"Pipe point {name} needs an existing floor")
        _number(node.get("x"), "Pipe x position", -500, 500)
        _number(node.get("y"), "Pipe y position", -500, 500)
        _number(node.get("height_mm", 150), "Pipe height", -2000, 10000)
        kind = node.get("kind", "junction")
        if kind not in {"junction", "sources", "emitters", "circuits"}:
            raise ValueError(f"Unknown pipe endpoint type at {name}")
        if kind != "junction" and node.get("equipment") not in rows[kind]:
            raise ValueError(f"Pipe point {name} references missing equipment")

    def location(node, room, side, offset):
        x, y = node["x"], node["y"]
        if side:
            anchor, _, _ = _side(rectangles[room], side)
            if side in ("north", "south"):
                y = anchor + offset * (1 if side == "north" else -1)
            else:
                x = anchor + offset * (1 if side == "west" else -1)
        height = node.get("height_mm", 150) / 1000
        return (x, y, rows["levels"][node["level"]]["elevation_m"] + height)

    for name, item in _entries(plan, "pipes").items():
        first, last = item.get("first"), item.get("last")
        circuit = item.get("circuit")
        run = item.get("run")
        if first not in nodes or last not in nodes or first == last:
            raise ValueError(f"Pipe {name} needs two different endpoints")
        if circuit not in rows["circuits"] or run not in {"supply", "return"}:
            raise ValueError(f"Pipe {name} needs a valid water circuit and run")
        room = item.get("room") or None
        if room and room not in rows["rooms"]:
            raise ValueError(f"Pipe {name} references an unknown room")
        side = item.get("wall", "")
        mounting = item.get("mounting", "free_running")
        anchored = mounting in {"on_wall", "in_wall"}
        if (mounting not in MOUNTING or anchored != bool(side)
                or (side and (side not in PLAN_SIDES or room not in rectangles))):
            raise ValueError(f"Pipe {name} needs a valid mounting location")
        if side and any(rows["rooms"][room]["level"] != nodes[key]["level"] for key in (first, last)):
            raise ValueError(f"Pipe {name} references a wall on a different floor")
        offset = _number(item.get("offset_mm", 40), "Wall offset", 0, 1000)
        diameter = _number(item.get("diameter_mm", 18), "Pipe diameter", 4, 250)
        wall_thickness = _number(item.get("wall_thickness_mm", 2.5), "Pipe wall thickness", 0.2, 15)
        material = item.get("material", "steel")
        if material not in {"steel", "copper", "pex", "ppr"}:
            raise ValueError(f"Unknown pipe material for {name}")
        if diameter <= 2 * wall_thickness:
            raise ValueError(f"Pipe wall thickness must be less than half the outside diameter for {name}")
        insulation = _number(item.get("insulation_mm", 9), "Pipe insulation", 0, 200)
        override = _number(item.get("loss_w_mk", 0), "Pipe conductance", 0, 100)
        displacement = (-offset if mounting == "in_wall" else offset) / 1000
        a = location(nodes[first], room, side, displacement)
        b = location(nodes[last], room, side, displacement)
        length = dist(a, b)
        if length <= TOLERANCE:
            raise ValueError(f"Pipe {name} has zero length")
        radius = diameter / 2000
        outer = radius + insulation / 1000
        resistance = log(outer / radius) / (2 * pi * 0.04) + 1 / (2 * pi * outer * 8)
        coefficient = override or 1 / resistance
        inner_radius = (diameter - 2 * wall_thickness) / 2000
        outer_radius = diameter / 2000
        water_volume_l = pi * inner_radius**2 * length * 1000
        metal_density = {"steel": 7850, "copper": 8960, "pex": 950, "ppr": 900}[material]
        metal_mass = pi * (outer_radius**2 - inner_radius**2) * length * metal_density
        pipes[name] = Pipe(name, circuit, run, first, last, length, coefficient * length / 1000,
                           room, mounting, (a, b), diameter, insulation, offset, side,
                           wall_thickness, material, water_volume_l, metal_mass)
    return pipes, nodes


def pipe_paths(pipes, nodes, emitters, sources):
    """Find a supply and return path for each connected emitter."""
    graphs = defaultdict(lambda: defaultdict(list))
    for pipe in pipes.values():
        graph = graphs[pipe.circuit, pipe.run]
        graph[pipe.first].append((pipe.last, pipe.name))
        graph[pipe.last].append((pipe.first, pipe.name))

    def root(node, circuit):
        item = nodes[node]
        if item.get("kind") == "circuits":
            return item.get("equipment") == circuit
        if item.get("kind") == "sources":
            source = sources[item["equipment"]]
            return not source.direct_zone and circuit in (source.loop, source.dhw_loop)
        return False

    routes = defaultdict(dict)
    for (circuit, run), graph in graphs.items():
        roots = sorted(node for node in graph if root(node, circuit))
        previous = {node: None for node in roots}
        queue = deque(roots)
        while queue:
            first = queue.popleft()
            for last, segment in graph[first]:
                if last not in previous:
                    previous[last] = first, segment
                    queue.append(last)
        for node, item in nodes.items():
            if item.get("kind") != "emitters" or node not in previous:
                continue
            emitter = item["equipment"]
            if emitters[emitter].loop != circuit:
                continue
            path = []
            point = node
            while previous[point]:
                parent, pipe = previous[point]
                path.append((pipe, parent, point))
                point = parent
            if path:
                if run in routes[emitter]:
                    raise ValueError(f"Radiator {emitter} has multiple connected {run} ports")
                routes[emitter][run] = tuple(reversed(path))
    return {name: route for name, route in routes.items() if route.keys() == {"supply", "return"}}

"use strict";

const iconPaths = {
    house:"M3 11 12 4l9 7v9H3z M9 20v-6h6v6",
    activity:"M2 12h5l3-7 4 14 3-7h5",
    plan:"M3 3h18v18H3z M12 3v9H3 M12 12h9 M15 12v9",
    room:"M3 3h18v18H3z M8 3v3 M3 8h3 M16 21v-3",
    layers:"m12 3 9 5-9 5-9-5z M3 12l9 5 9-5 M3 16l9 5 9-5",
    save:"M4 3h13l3 3v15H4z M8 3v6h8V3 M8 21v-7h8v7",
    play:"M7 4l14 8-14 8z",
    upload:"M12 16V4 M7 9l5-5 5 5 M4 16v4h16v-4",
    download:"M12 4v12 M7 11l5 5 5-5 M4 17v3h16v-3",
    fit:"M3 9V3h6 M15 3h6v6 M21 15v6h-6 M9 21H3v-6",
    cursor:"m4 3 16 9-8 2-2 7z",
    radiator:"M3 7h18v10H3z M6 7v10 M10 7v10 M14 7v10 M18 7v10 M5 17v3 M19 17v3",
    pipe:"M3 5h8a3 3 0 0 1 3 3v8h7 M3 9h7v7a4 4 0 0 0 4 4h7",
    junction:"M12 3v18 M3 12h18 M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z",
    boiler:"M6 3h12v17H6z M9 16h6 M12 7c0 2-3 2-3 4a3 3 0 0 0 6 0c0-1-1-3-3-4z",
    fan:"M12 12c-7-5-4-10-1-8 2 1 2 4 1 8z M12 12c8-3 11 2 8 4-2 1-5-1-8-4z M12 12c-1 8-6 10-7 6-1-2 2-4 7-6z",
    sun:"M12 2v3 M12 19v3 M2 12h3 M19 12h3 M5 5l2 2 M17 17l2 2 M19 5l-2 2 M7 17l-2 2 M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0z",
    window:"M4 4h16v16H4z M12 4v16 M4 12h16",
    thermometer:"M10 14.5V5a2 2 0 0 1 4 0v9.5a4 4 0 1 1-4 0z M12 8v10",
    power:"m13 2-8 12h6l-1 8 9-12h-7z",
    tank:"M6 7a6 3 0 0 1 12 0v10a6 3 0 0 1-12 0z M6 7a6 3 0 0 0 12 0 M12 4V2 M12 20v2",
    exchange:"M4 7h15m-4-4 4 4-4 4 M20 17H5m4-4-4 4 4 4"
};
const sectionIcons = {
    levels:"layers",rooms:"room",surfaces:"plan",windows:"window",circuits:"pipe",emitters:"radiator",
    sources:"boiler",outdoor_units:"fan",solar:"sun",weather:"sun",events:"activity"
};
function icon(name) {
    const graphic = svg("svg", {viewBox:"0 0 24 24",class:"icon","aria-hidden":"true"});
    graphic.append(svg("path",{d:iconPaths[name] || iconPaths.plan}));
    return graphic;
}
for (const node of document.querySelectorAll("[data-icon]")) node.prepend(icon(node.dataset.icon));

const PLAN_SCALE = 48;
const sides = ["north","east","south","west"];
const initialBox = [-48,-48,760,500];
let planBox = initialBox.slice(), activeTool = "select", routePoint = "", drag = null;
let fitted = false, planFloor = "";
let selectedPipes = new Set();

function planData() {
    project.floorplan ??= {};
    for (const section of ["rooms","nodes","pipes"]) project.floorplan[section] ??= {};
    return project.floorplan;
}
function renameReferences(section,before,after) {
    for (const collection of Object.keys(catalogue.presets)) {
        for (const item of project[collection] || []) {
            for (const [key,target] of Object.entries(references)) {
                if (target === section && item[key] === before) item[key] = after;
            }
        }
    }
    const plan = planData();
    if (section === "rooms" && plan.rooms[before]) {
        plan.rooms[after] = plan.rooms[before]; delete plan.rooms[before];
    }
    if (section === "levels" && planFloor === before) planFloor = after;
    for (const node of Object.values(plan.nodes)) {
        if (section === "levels" && node.level === before) node.level = after;
        if (node.kind === section && node.equipment === before) node.equipment = after;
    }
    for (const pipe of Object.values(plan.pipes)) {
        if (section === "rooms" && pipe.room === before) pipe.room = after;
        if (section === "circuits" && pipe.circuit === before) pipe.circuit = after;
    }
}
function list(section) { return project[section] ??= []; }
function find(section,id) { return list(section).find(item => item.id === id); }
function value(section,row,key) { return row[key] ?? catalogue.presets[section][row.preset]?.[key]; }
function unique(prefix,ids) {
    let index = 1;
    while (ids.includes(`${prefix}_${index}`)) index++;
    return `${prefix}_${index}`;
}
function optionList(select,values,selected) {
    select.replaceChildren(...values.map(value => {
        const option = el("option",value || "Select…");
        option.value = value;
        return option;
    }));
    select.value = selected;
}
function planMessage(text,error=false) {
    $("plan-status").textContent = text;
    $("plan-status").classList.toggle("error",error);
}
function syncPlan() {
    if (!project) return;
    planData();
    const floors = list("levels").map(row => row.id);
    if (!floors.includes(planFloor)) planFloor = floors[0] || "";
    optionList($("plan-level"),floors,planFloor);
    const circuits = list("circuits").map(row => row.id);
    const current = $("pipe-circuit").value;
    optionList($("pipe-circuit"),circuits,circuits.includes(current) ? current : circuits[0] || "");
    if (!fitted) {
        planBox = fitBox(planData(), roomMap(project), planFloor);
        fitted = true;
    }
    drawPlan();
}
function roomMap(data) {
    return Object.fromEntries((data.rooms || []).map(row => [row.id,{
        ...catalogue.presets.rooms[row.preset], ...row
    }]));
}
function fitBox(plan,rooms,floor) {
    const xs=[],ys=[];
    for (const [name,position] of Object.entries(plan.rooms || {})) {
        const room = rooms[name];
        if (room?.level !== floor) continue;
        xs.push(position.x,position.x+room.length_m);
        ys.push(position.y,position.y+room.width_m);
    }
    for (const point of Object.values(plan.nodes || {})) if (point.level === floor) {
        xs.push(point.x); ys.push(point.y);
    }
    if (!xs.length) return initialBox.slice();
    const x = Math.min(...xs), y = Math.min(...ys);
    return [(x-1.3)*PLAN_SCALE,(y-1.3)*PLAN_SCALE,
        Math.max(5,Math.max(...xs)-x+2.6)*PLAN_SCALE,
        Math.max(4,Math.max(...ys)-y+2.6)*PLAN_SCALE];
}
function snap(number) { return Math.round(number*4)/4; }
function mousePoint(event) {
    const transformed = new DOMPoint(event.clientX,event.clientY).matrixTransform(
        $("plan-svg").getScreenCTM().inverse());
    return {x:snap(transformed.x/PLAN_SCALE),y:snap(transformed.y/PLAN_SCALE)};
}
function line(first,last,attributes) {
    return svg("line",{x1:first.x*PLAN_SCALE,y1:first.y*PLAN_SCALE,
        x2:last.x*PLAN_SCALE,y2:last.y*PLAN_SCALE,...attributes});
}
function wallLine(position,room,side) {
    const {x,y} = position, east = x+room.length_m, south = y+room.width_m;
    if (side === "north") return [{x,y},{x:east,y}];
    if (side === "south") return [{x,y:south},{x:east,y:south}];
    if (side === "east") return [{x:east,y},{x:east,y:south}];
    return [{x,y},{x,y:south}];
}
function pipePoints(plan,rooms,pipe) {
    let first = {...plan.nodes[pipe.first]}, last = {...plan.nodes[pipe.last]};
    const room = rooms[pipe.room], placement = plan.rooms[pipe.room];
    if (placement && room && sides.includes(pipe.wall)) {
        const [a] = wallLine(placement,room,pipe.wall);
        const axis = ["north","south"].includes(pipe.wall) ? "y" : "x";
        const direction = (["north","west"].includes(pipe.wall) ? 1 : -1)
            * (pipe.mounting === "in_wall" ? -1 : 1);
        first[axis] = last[axis] = a[axis]+direction*(pipe.offset_mm ?? 40)/1000;
    }
    const direction = pipe.run === "return" ? 1 : -1;
    const length = Math.hypot(last.x-first.x,last.y-first.y);
    if (length) {
        const dx = direction*(first.y-last.y)/length*.105;
        const dy = direction*(last.x-first.x)/length*.105;
        first.x += dx; last.x += dx; first.y += dy; last.y += dy;
    }
    return [first,last];
}
function textAt(x,y,text,className) {
    const node = svg("text",{x:x*PLAN_SCALE,y:y*PLAN_SCALE,class:className});
    node.textContent = text;
    return node;
}
function paint(canvas,plan,rooms,floor,box,liveSources=null) {
    const drawing = !liveSources;
    canvas.setAttribute("viewBox",box.join(" "));
    canvas.replaceChildren();
    const defs = svg("defs"), gridId = canvas.id+"-grid";
    const pattern = svg("pattern",{id:gridId,width:12,height:12,patternUnits:"userSpaceOnUse"});
    pattern.append(svg("path",{d:"M12 0H0v12",fill:"none",stroke:"var(--grid)","stroke-width":".65"}));
    defs.append(pattern); canvas.append(defs);
    canvas.append(svg("rect",{x:box[0],y:box[1],width:box[2],height:box[3],fill:`url(#${gridId})`}));
    for (const [name,location] of Object.entries(plan.rooms || {})) {
        const room = rooms[name];
        if (room?.level !== floor) continue;
        const position = drawing && drag?.name === name ? drag.position : location;
        const area = svg("rect",{x:position.x*PLAN_SCALE,y:position.y*PLAN_SCALE,
            width:room.length_m*PLAN_SCALE,height:room.width_m*PLAN_SCALE,
            rx:2,class:`plan-room ${room.preset === "unheated" ? "unheated" : ""} ${location.derive ? "" : "legacy"}`,
            "data-room":name});
        const label = textAt(position.x+.16,position.y+.36,human(name),"plan-label");
        const reading = textAt(position.x+.16,position.y+.73,
            `${format(room.length_m)} × ${format(room.width_m)} m`,"plan-meta");
        if (drawing) canvas.append(area,label,reading);
        else {
            reading.setAttribute("class","live-room-temperature");
            canvas.append(livePlanItem("rooms",name,"live-room-item",area,label,reading));
        }
        for (const side of sides) {
            const [first,last] = wallLine(position,room,side);
            if (drawing && activeTool === "wall") {
                canvas.append(line(first,last,{class:"plan-wall","data-room":name,"data-wall":side}));
            }
            for (const opening of location.walls?.[side]?.openings || []) {
                const axis = first.y === last.y ? "x" : "y";
                const middle = first[axis]+opening.distance;
                const beginning = {...first,[axis]:middle-opening.width/2};
                const ending = {...first,[axis]:middle+opening.width/2};
                const type = opening.type || "window";
                canvas.append(line(beginning,ending,{class:"plan-opening plan-opening-"+type}));
                if (type === "door" || type === "entry_door") {
                    const hingeAtEnd = opening.swing_side === "right";
                    const hinge = hingeAtEnd ? ending : beginning;
                    const tangent = axis === "x" ? {x:hingeAtEnd ? -1 : 1,y:0} :
                        {x:0,y:hingeAtEnd ? -1 : 1};
                    const inward = side === "north" ? {x:0,y:1} : side === "south" ? {x:0,y:-1} :
                        side === "east" ? {x:-1,y:0} : {x:1,y:0};
                    const openness = Math.max(0,Math.min(100,opening.openness_pct ?? 0))*Math.PI/200;
                    const direction = opening.swing_direction === "out_of_room" ? -1 : 1;
                    const dx = (tangent.x*Math.cos(openness)+inward.x*direction*Math.sin(openness))*opening.width;
                    const dy = (tangent.y*Math.cos(openness)+inward.y*direction*Math.sin(openness))*opening.width;
                    canvas.append(line(hinge,{x:hinge.x+dx,y:hinge.y+dy},
                        {class:"plan-door-leaf"}));
                }
            }
        }
    }
    for (const [name,pipe] of Object.entries(plan.pipes || {})) {
        if (!plan.nodes?.[pipe.first] || !plan.nodes?.[pipe.last]) continue;
        const [first,last] = pipePoints(plan,rooms,pipe);
        if (first.level !== floor && last.level !== floor) continue;
        const riser = first.level !== last.level;
        const base = riser && first.level !== floor ? last : first;
        const visibleFirst = riser ? {...base,x:base.x+(pipe.run === "supply" ? -.11 : .11)} : base;
        const visibleLast = riser ? {...visibleFirst,x:visibleFirst.x+.26,y:visibleFirst.y+.26} : last;
        const target = line(visibleFirst,visibleLast,{class:"pipe-target","data-pipe":name});
        const path = line(visibleFirst,visibleLast,{class:`plan-pipe ${pipe.run} ${riser ? "riser" : ""}`,
            "pointer-events":"none"});
        if (drawing && selectedPipes.has(name)) path.style.strokeWidth = "6";
        if (drawing) {
            target.append(svg("title"));
            target.firstChild.textContent = name;
            canvas.append(path,target);
        } else {
            const label = textAt((visibleFirst.x+visibleLast.x)/2,(visibleFirst.y+visibleLast.y)/2-.08,"","live-pipe-label");
            label.setAttribute("text-anchor","middle");
            canvas.append(livePlanItem("pipes",name,"live-pipe-item",path,target,label));
        }
    }
    for (const [name,node] of Object.entries(plan.nodes || {})) {
        if (node.level !== floor) continue;
        if (node.kind === "junction") {
            canvas.append(svg("circle",{cx:node.x*PLAN_SCALE,cy:node.y*PLAN_SCALE,r:routePoint === name ? 5 : 3,
                class:`plan-point ${routePoint === name ? "active" : ""}`,"data-node":name}));
        } else {
            const x=node.x*PLAN_SCALE, y=node.y*PLAN_SCALE;
            const equipment = svg("rect",{x:x-9,y:y-9,width:18,height:18,rx:4,
                class:"plan-equipment","data-node":name});
            const sourcePreset = drawing ? find("sources",node.equipment)?.preset : liveSources[node.equipment]?.preset;
            const symbol = icon(node.kind === "sources" && sourcePreset?.includes("split") ? "fan" :
                {emitters:"radiator",sources:"boiler",circuits:"tank"}[node.kind]);
            for (const [key,value] of Object.entries({x:x-7,y:y-7,width:14,height:14,class:"icon plan-device-icon"})) symbol.setAttribute(key,value);
            const label = textAt(node.x+.24,node.y-.21,human(node.equipment),"plan-meta");
            const source = node.kind === "sources"
                ? (drawing ? find("sources",node.equipment) : liveSources[node.equipment]) : null;
            const facing = source?.preset === "indoor_split"
                ? (node.facing_deg ?? roomCenterFacing(node,source.room || "")) : null;
            const facingMarks = [];
            if (facing !== null && Number.isFinite(facing)) {
                const angle = facing*Math.PI/180, dx = Math.sin(angle), dy = -Math.cos(angle);
                const start = {x:node.x+dx*.19,y:node.y+dy*.19};
                const end = {x:node.x+dx*.48,y:node.y+dy*.48};
                const back = {x:end.x-dx*.10,y:end.y-dy*.10};
                const sideX = Math.cos(angle)*.055, sideY = Math.sin(angle)*.055;
                facingMarks.push(line(start,end,{class:"plan-facing"}),
                    line(end,{x:back.x+sideX,y:back.y+sideY},{class:"plan-facing"}),
                    line(end,{x:back.x-sideX,y:back.y-sideY},{class:"plan-facing"}));
            }
            if (drawing) canvas.append(equipment,symbol,...facingMarks,label);
            else {
                const reading = textAt(node.x+.24,node.y+.12,"","live-device-reading");
                canvas.append(livePlanItem(node.kind,node.equipment,"live-device-item",equipment,symbol,
                    ...facingMarks,label,reading));
            }
        }
    }
    if (drawing && drag?.preview) {
        const box = drag.preview;
        canvas.append(svg("rect",{x:box.x*PLAN_SCALE,y:box.y*PLAN_SCALE,
            width:box.width*PLAN_SCALE,height:box.height*PLAN_SCALE,class:"plan-room preview"}));
    }
}
function drawPlan() {
    if (!project || !catalogue) return;
    const canvas = $("plan-svg");
    canvas.dataset.tool = activeTool;
    $("pipe-tools").hidden = activeTool !== "pipe";
    const plan = planData();
    const preview = drag?.node ? {...plan,nodes:{...plan.nodes,
        [drag.node]:{...plan.nodes[drag.node],...drag.position}}} : plan;
    paint(canvas,preview,roomMap(project),planFloor,planBox);
}
function dialog(title,fields,save,other=[]) {
    const form = $("plan-form");
    $("plan-dialog-title").textContent = title;
    form.replaceChildren(...fields);
    const footer = el("div"); footer.className = "toolbar";
    const submit = el("button","Save"); submit.type = "submit"; submit.className = "primary";
    const cancel = el("button","Cancel"); cancel.type = "button";
    cancel.onclick = () => $("plan-dialog").close();
    footer.append(submit,cancel,...other);
    form.append(footer);
    form.onsubmit = event => {
        event.preventDefault();
        const error = save();
        if (error) { $("plan-dialog-error").textContent = error; return; }
        $("plan-dialog").close();
        redrawForms();
    };
    const error = el("div"); error.id = "plan-dialog-error"; error.setAttribute("role","alert");
    form.append(error);
    if (!$("plan-dialog").open) $("plan-dialog").showModal();
}
function extraButton(label,action) {
    const button = el("button",label); button.type = "button"; button.onclick = action;
    return button;
}
function configField(section,row,key,defaultValue) {
    let options = references[key] ? ["",...(project[references[key]] || []).map(item => item.id)] : null;
    if (key === "boundary") options = ["outdoors","room","ground","adiabatic"];
    if (key === "mode_rule") options = ["largest_demand","heat_priority","cool_priority"];
    if (key === "allocation") options = ["proportional","in_order"];
    const label = section === "events" && key === "opening_fraction"
        ? "Opening fraction (0–1)" : human(key);
    return input(label,row[key] ?? defaultValue,next => row[key]=next,options,
        key === "start_s" || (section === "events" && key === "duration_s"));
}
function editObject(section,id) {
    if (section === "settings") {
        const draft = {...(project.settings || {})};
        const keys=new Set([...Object.keys(catalogue.settings),...Object.keys(draft)]);
        const fields=[...keys].map(key => configField("settings",draft,key,catalogue.settings[key]));
        dialog("House location and simulation settings",fields,() => {
            project.settings = {...project.settings,...draft};
        });
        return;
    }
    const rows = list(section), index = rows.findIndex(row => row.id === id);
    if (index < 0) return;
    const row = rows[index], draft = {...row}, presets = catalogue.presets[section];
    function show() {
        const defaults = presets[draft.preset] || {};
        const fields = [
            input("Object ID",draft.id,next => draft.id=next),
            input("Preset",draft.preset,next => { draft.preset=next; show(); },
                Object.keys(presets),false,show)
        ];
        const advanced = el("fieldset");
        advanced.append(el("legend","Object settings"));
        const keys = new Set([...Object.keys(defaults),...Object.keys(draft)]);
        keys.delete("id"); keys.delete("preset");
        for (const key of keys) {
            const value = draft[key] ?? defaults[key];
            if (value === undefined || (value && typeof value === "object")) continue;
            advanced.append(configField(section,draft,key,defaults[key]));
        }
        fields.push(advanced);
        dialog((names[section] || human(section))+" · "+draft.id,fields,() => {
            if (!/^[a-zA-Z0-9_-]{1,64}$/.test(draft.id) ||
                rows.some((item,itemIndex) => itemIndex !== index && item.id === draft.id)) {
                return "Enter a unique ID using letters, digits, underscores or hyphens.";
            }
            if (draft.id !== row.id) renameReferences(section,row.id,draft.id);
            Object.assign(row,draft);
        },[
            extraButton("Duplicate",() => {
                let suffix=2;
                while (rows.some(item => item.id===draft.id+"_"+suffix)) suffix++;
                rows.push({...draft,id:draft.id+"_"+suffix});
                $("plan-dialog").close(); redrawForms(); openObjectManager(section);
            }),
            extraButton("Remove",() => {
                rows.splice(index,1);
                $("plan-dialog").close(); redrawForms(); openObjectManager(section);
            })
        ]);
    }
    show();
}
function objectManagerDialog(section) {
    const groups = ["settings",...Object.keys(catalogue.presets)];
    const picker = el("div"), filters=el("div"), listBox=el("div");
    filters.className="toolbar";
    listBox.style.maxHeight="52vh"; listBox.style.overflowY="auto";
    const group=el("select");
    for (const key of groups) {
        const option=el("option",key==="settings" ? "House settings" : (names[key] || human(key)));
        option.value=key; group.append(option);
    }
    group.value=groups.includes(section) ? section : "settings";
    const search=el("input"); search.type="search"; search.placeholder="Find an object…";
    search.setAttribute("aria-label","Find an object");
    filters.append(group,search);
    function render() {
        const key=group.value, query=search.value.trim().toLowerCase();
        listBox.replaceChildren();
        if (key==="settings") {
            const card=el("fieldset");
            card.append(el("legend","House settings"),
                el("p","Location, orientation, weather timing, and simulation controls."),
                extraButton("Edit house settings",()=>editObject("settings")));
            listBox.append(card); return;
        }
        const rows=list(key), presets=catalogue.presets[key];
        const addBar=el("div"); addBar.className="toolbar";
        const preset=el("select"); preset.setAttribute("aria-label","Preset for "+(names[key] || human(key)));
        for (const value of Object.keys(presets)) {
            const option=el("option",human(value)); option.value=value; preset.append(option);
        }
        addBar.append(preset,extraButton("Add object",() => {
            let suffix=rows.length+1;
            while (rows.some(row => row.id===preset.value+"_"+suffix)) suffix++;
            const id=preset.value+"_"+suffix;
            rows.push({id,preset:preset.value});
            redrawForms(); editObject(key,id);
        }));
        listBox.append(addBar);
        const matches=rows.filter(row=>row.id.toLowerCase().includes(query));
        if (!matches.length) listBox.append(el("p",query ? "No matching objects." : "No objects yet."));
        for (const row of matches) {
            const card=el("fieldset");
            card.append(el("legend",row.id+" · "+human(row.preset)),
                extraButton("Edit settings",()=>editObject(key,row.id)));
            listBox.append(card);
        }
    }
    group.onchange=render; search.oninput=render; render();
    picker.append(filters,listBox);
    const form=$("plan-form");
    $("plan-dialog-title").textContent="Configure house objects";
    const footer=el("div"); footer.className="toolbar";
    footer.append(extraButton("Close",()=>$("plan-dialog").close()));
    form.replaceChildren(picker,footer);
    form.onsubmit=event=>event.preventDefault();
    if (!$("plan-dialog").open) $("plan-dialog").showModal();
}
function openObjectManager(section="settings") { objectManagerDialog(section); }
$("#objects").onclick=()=>openObjectManager();
function selectTool(tool) {
    activeTool = tool; routePoint = ""; selectedPipes.clear();
    for (const button of document.querySelectorAll("button[data-tool]")) {
        button.setAttribute("aria-pressed",button.dataset.tool === tool);
    }
    const tips = {
        select:"Click for settings. Drag rooms or connection points to move them. Shift-click pipes to select several.",
        room:"Drag a rectangle to draw a room.",
        wall:"Click a wall edge to choose a construction or add an opening.",
        equipment:"Click a position to place a radiator, boiler, or circuit connection.",
        pipe:"Select a circuit and click two points. Click an existing point to connect. Continue to add bends.",
        junction:"Click a drawn pipe to split it and create a connection point."
    };
    planMessage(tips[tool]);
    drawPlan();
}
for (const button of document.querySelectorAll("button[data-tool]")) {
    button.onclick = () => selectTool(button.dataset.tool);
}
$("plan-level").onchange = () => {
    planFloor = $("plan-level").value;
    if (activeTool !== "pipe") routePoint = "";
    planBox = fitBox(planData(),roomMap(project),planFloor); drawPlan();
};
$("fit-plan").onclick = () => { planBox = fitBox(planData(),roomMap(project),planFloor); drawPlan(); };
for (const [id,factor] of [["zoom-in",.8],["zoom-out",1.25]]) {
    $(id).onclick = () => {
        const [x,y,w,h] = planBox;
        planBox = [x+(w-w*factor)/2,y+(h-h*factor)/2,w*factor,h*factor];
        drawPlan();
    };
}
$("end-route").onclick = () => { routePoint = ""; drawPlan(); };
$("pipe-circuit").onchange = $("pipe-run").onchange = () => { routePoint = ""; drawPlan(); };
$("blank").onclick = () => {
    if (!catalogue) return;
    dialog("Start a blank house",[
        el("p","Replace the unsaved editor contents with a ground floor, a radiator circuit and sample weather.")
    ],() => {
        project = {
            settings:{warmup_days:0},
            levels:[{id:"ground",preset:"storey"}],
            circuits:[{id:"radiators",preset:"radiator_loop"}],
            weather:structuredClone(catalogue.example.weather)
        };
        fitted = false; planFloor = "ground";
        message("Blank house ready. Draw the first room.");
    });
};
$("arrange-plan").onclick = () => {
    if (!project || !planFloor) return;
    const plan = planData(), rooms = roomMap(project);
    const placed = Object.entries(plan.rooms).filter(([name]) => rooms[name]?.level === planFloor);
    let x = placed.length ? Math.max(...placed.map(([name,p]) => p.x+rooms[name].length_m))+.5 : 0;
    const missing = Object.entries(rooms).filter(([id,row]) => row.level === planFloor && !plan.rooms[id]);
    for (const [id,room] of missing) {
        plan.rooms[id] = {x,y:0,derive:false};
        x += room.length_m+.5;
    }
    planBox = fitBox(plan,rooms,planFloor);
    planMessage(missing.length ? "Rooms placed for editing. Existing thermal surfaces were kept." :
        "All rooms on this floor are already placed.");
    drawPlan();
};
$("add-level").onclick = () => {
    if (!project) return;
    const floors = list("levels");
    const draft = {id:unique("floor",floors.map(row => row.id)),
        elevation_m:floors.length ? Math.max(...floors.map(row => row.elevation_m ?? 0))+2.7 : 0,height_m:2.7};
    dialog("Add floor",[
        input("Floor ID",draft.id,next => draft.id=next),
        input("Elevation (m)",draft.elevation_m,next => draft.elevation_m=next),
        input("Default room height (m)",draft.height_m,next => draft.height_m=next),
    ],() => {
        if (!/^[a-zA-Z0-9_-]+$/.test(draft.id) || find("levels",draft.id) ||
            draft.elevation_m === null || !(draft.height_m > 0)) return "Enter a unique ID and a positive room height.";
        floors.push({preset:"storey",...draft}); planFloor=draft.id; planBox=initialBox.slice();
    });
};

function roomDialog(name,box) {
    const existing = name ? find("rooms",name) : null;
    const location = name ? planData().rooms[name] : box;
    const draft = existing ? {...catalogue.presets.rooms[existing.preset],...existing} : {
        id:unique("room",list("rooms").map(row => row.id)),preset:"conditioned",level:planFloor,
        length_m:box.width,width_m:box.height
    };
    const placement = {...location,derive:existing ? Boolean(location.derive) : true};
    const fields = [];
    if (!existing) fields.push(input("Room ID",draft.id,next => draft.id=next));
    fields.push(input("Room type",draft.preset,next => draft.preset=next,["conditioned","unheated"]),
        input("Floor",draft.level,next => draft.level=next,list("levels").map(row => row.id)),
        input("Length east–west (m)",draft.length_m,next => draft.length_m=next),
        input("Width north–south (m)",draft.width_m,next => draft.width_m=next),
        input("Room height (m, 0 uses floor default)",draft.height_m ?? 0,next => draft.height_m=next),
        input("Generate walls from the drawing",placement.derive,next => placement.derive=next));
    const roomSettings=el("fieldset"); roomSettings.append(el("legend","Thermal and air settings"));
    const roomDefaults=catalogue.presets.rooms[draft.preset] || {};
    for (const key of new Set([...Object.keys(roomDefaults),...Object.keys(draft)])) {
        if (["id","preset","level","length_m","width_m","height_m"].includes(key)) continue;
        if (draft[key] !== undefined || roomDefaults[key] !== undefined) {
            roomSettings.append(configField("rooms",draft,key,roomDefaults[key]));
        }
    }
    fields.push(roomSettings);
    const remove = existing ? [extraButton("Remove from plan",() => {
        if (Object.values(planData().pipes).some(pipe => pipe.room === name && pipe.wall)) {
            $("plan-dialog-error").textContent = "Move or remove the pipes anchored to this room's walls first.";
            return;
        }
        delete planData().rooms[name];
        $("plan-dialog").close(); redrawForms();
    })] : [];
    dialog(existing ? human(name) : "New room",fields,() => {
        if (!/^[a-zA-Z0-9_-]{1,64}$/.test(draft.id) || (!existing && find("rooms",draft.id)) ||
            !(draft.length_m >= .25 && draft.width_m >= .25) || (draft.height_m ?? 0) < 0) {
            return "Enter a unique room ID and valid dimensions.";
        }
        if (existing) Object.assign(existing,draft);
        else list("rooms").push(draft);
        planData().rooms[draft.id] = {x:placement.x,y:placement.y,derive:placement.derive,walls:placement.walls || {}};
    },remove);
}
function wallDialog(name,side) {
    const room = roomMap(project)[name], placement = planData().rooms[name];
    if (!room || !placement) return;
    const wall = structuredClone(placement.walls?.[side] || {construction:"insulated_brick",openings:[]});
    const extent = ["north","south"].includes(side) ? room.length_m : room.width_m;
    function show() {
        const fields = [input("Exterior construction",wall.construction,next => wall.construction=next,
            catalogue.wall_types.filter(type => !["insulated_ceiling","insulated_floor","partition"].includes(type))),
            input("Buried wall height (m)",wall.ground_height_m ?? 0,next => wall.ground_height_m=next)];
        wall.openings ??= [];
        if (!placement.derive) fields.push(el("small","Enable generated walls in the room settings to use these values in the simulation."));
        for (const [index,opening] of wall.openings.entries()) {
            const entry = el("fieldset"); entry.append(el("legend",`Opening ${index+1}`),
                input("Opening type",opening.type || "window",next => {
                    opening.type=next;
                    if (next === "window") opening.preset ??= "double_glazed";
                    show();
                },["window","door","portal","entry_door"],false,show),
                input("Centre from first end (m)",opening.distance,next => opening.distance=next),
                input("Width (m)",opening.width,next => opening.width=next),
                input("Height (m)",opening.height,next => opening.height=next));
            if ((opening.type || "window") === "window") {
                entry.append(input("Window type",opening.preset || "double_glazed",
                    next => opening.preset=next,Object.keys(catalogue.presets.windows)));
            }
            if (opening.type === "door") {
                entry.append(input("Door open (%)",opening.openness_pct ?? 0,
                    next => opening.openness_pct=next),
                    input("Hinge side (viewed from room)",opening.swing_side || "left",next => opening.swing_side=next,
                        ["left","right"]),
                    input("Swing direction",opening.swing_direction || "into_room",
                        next => opening.swing_direction=next,["into_room","out_of_room"]));
            }
            if (opening.type === "entry_door") {
                opening.preset ??= "insulated_entry_door";
                entry.append(input("Door construction",opening.preset,next => {
                    opening.preset=next;
                    const preset = catalogue.presets.windows[next];
                    opening.u_w_m2k=preset.u_w_m2k;
                    opening.area_heat_capacity_kwh_m2k=preset.area_heat_capacity_kwh_m2k;
                    show();
                },["insulated_entry_door","standard_entry_door","older_entry_door"]),
                    input("U-value (W/m²K)",opening.u_w_m2k ?? catalogue.presets.windows[opening.preset].u_w_m2k,
                        next => opening.u_w_m2k=next),
                    input("Heat capacity (kWh/m²K)",opening.area_heat_capacity_kwh_m2k ??
                        catalogue.presets.windows[opening.preset].area_heat_capacity_kwh_m2k,
                        next => opening.area_heat_capacity_kwh_m2k=next),
                    input("Hinge side (viewed from room)",opening.swing_side || "left",next => opening.swing_side=next,
                        ["left","right"]),
                    input("Swing direction",opening.swing_direction || "into_room",
                        next => opening.swing_direction=next,["into_room","out_of_room"]));
            }
            entry.append(extraButton("Remove opening",() => { wall.openings.splice(index,1); show(); }));
            fields.push(entry);
        }
        dialog(`${human(name)} · ${side} wall`,fields,() => {
            if (wall.openings.some(o => !(o.width > 0 && o.height > 0 && o.distance >= o.width/2 &&
                o.distance+o.width/2 <= extent))) return "Every opening must fit inside this wall.";
            placement.walls ??= {}; placement.walls[side] = wall;
        },[
            extraButton("Add window",() => {
                wall.openings.push({type:"window",preset:"double_glazed",distance:extent/2,
                    width:Math.min(1.2,extent),height:1.1}); show();
            }),
            extraButton("Add door",() => {
                wall.openings.push({type:"door",distance:extent/2,width:Math.min(.9,extent),
                    height:2.0,openness_pct:0}); show();
            }),
            extraButton("Add portal",() => {
                wall.openings.push({type:"portal",distance:extent/2,width:Math.min(.9,extent),
                    height:2.0}); show();
            }),
            extraButton("Add insulated entry door",() => {
                wall.openings.push({type:"entry_door",preset:"insulated_entry_door",distance:extent/2,
                    width:Math.min(.9,extent),height:2.0,u_w_m2k:1.0,
                    area_heat_capacity_kwh_m2k:.025,swing_side:"left",swing_direction:"into_room"}); show();
            })]);
    }
    show();
}

function containingRoom(point) {
    const rooms = roomMap(project);
    return Object.entries(planData().rooms).find(([name,location]) => {
        const room = rooms[name];
        return room?.level === planFloor && point.x >= location.x && point.y >= location.y &&
            point.x <= location.x+room.length_m && point.y <= location.y+room.width_m;
    })?.[0] || "";
}
function roomCenterFacing(point, roomName) {
    const room = roomMap(project)[roomName], placement = planData().rooms[roomName];
    if (!room || !placement) return 0;
    const dx = placement.x + room.length_m/2 - point.x;
    const dy = placement.y + room.width_m/2 - point.y;
    return (Math.atan2(dx,-dy)*180/Math.PI+360)%360;
}
function addPoint(point,props={}) {
    const nodes = planData().nodes;
    const id = unique("point",Object.keys(nodes));
    nodes[id] = {...point,level:planFloor,height_mm:150,kind:"junction",...props};
    return id;
}
function equipmentDialog(point,nodeId) {
    const current = planData().nodes[nodeId];
    const draft = current ? {...current} : {kind:"emitters",equipment:"",level:planFloor,height_mm:150,...point};
    let preset = "panel_radiator", radiatorId = unique("radiator",list("emitters").map(row => row.id));
    let sourcePreset = "gas_on_off", sourceId = unique("heater",list("sources").map(row => row.id));
    let circuit = list("circuits")[0]?.id || "", room = containingRoom(point);
    function show() {
        const fields = [
            input("Equipment type",draft.kind,next => { draft.kind=next; draft.equipment=""; },
                ["emitters","sources","circuits"],false,show),
            input("Equipment",draft.equipment,next => draft.equipment=next,
                ["",...list(draft.kind).map(row => row.id),
                 ...({emitters:["+ New radiator"],sources:["+ New heat source"]}[draft.kind] || [])],
                false,show),
            input("Connection height above floor (mm)",draft.height_mm,next => draft.height_mm=next)
        ];
        if (draft.equipment === "+ New radiator") fields.push(
            input("Radiator ID",radiatorId,next => radiatorId=next),
            input("Radiator preset",preset,next => preset=next,
                ["panel_radiator","column_radiator","towel_radiator","custom_radiator"]),
            input("Room",room,next => room=next,list("rooms").map(row => row.id)),
            input("Water circuit",circuit,next => circuit=next,list("circuits").map(row => row.id)));
        if (draft.equipment === "+ New heat source") fields.push(
            input("Heat source ID",sourceId,next => sourceId=next),
            input("Preset",sourcePreset,next => sourcePreset=next,["gas_on_off","gas_modulating","air_to_water"]),
            input("Water circuit",circuit,next => circuit=next,list("circuits").map(row => row.id)));
        const placedSource = draft.kind === "sources" ? find("sources",draft.equipment) : null;
        if (placedSource?.preset === "indoor_split") fields.push(input("A/C facing (° clockwise from north)",
            draft.facing_deg ?? roomCenterFacing(draft,placedSource.room || room),
            next => draft.facing_deg=next));
        if (draft.kind === "circuits" && !list("circuits").length) {
            fields.push(el("small","Add a water circuit from Configure objects first."));
        }
        dialog(current ? "Equipment connection" : "Place equipment",fields,() => {
            if (!draft.equipment || !Number.isFinite(draft.height_mm) || draft.height_mm < -2000 || draft.height_mm > 10000) {
                return "Choose the equipment and a valid connection height.";
            }
            const selectedSource = draft.kind === "sources" ? find("sources",draft.equipment) : null;
            if (selectedSource?.preset === "indoor_split") {
                draft.facing_deg ??= roomCenterFacing(draft,selectedSource.room || room);
                if (!Number.isFinite(draft.facing_deg) || draft.facing_deg < 0 || draft.facing_deg > 360) {
                    return "Set an A/C facing direction from 0° to 360° clockwise from north.";
                }
            } else {
                delete draft.facing_deg;
            }
            if (draft.equipment === "+ New radiator") {
                if (!/^[a-zA-Z0-9_-]{1,64}$/.test(radiatorId) || find("emitters",radiatorId) || !room || !circuit) {
                    return "Enter a unique radiator ID and select a room and water circuit.";
                }
                list("emitters").push({id:radiatorId,preset,room,circuit});
                draft.equipment=radiatorId;
            }
            if (draft.equipment === "+ New heat source") {
                if (!/^[a-zA-Z0-9_-]{1,64}$/.test(sourceId) || find("sources",sourceId) || !circuit) {
                    return "Enter a unique heat source ID and choose a water circuit.";
                }
                list("sources").push({id:sourceId,preset:sourcePreset,circuit});
                draft.equipment=sourceId;
            }
            if (current) Object.assign(current,draft);
            else addPoint(point,draft);
        },[
            ...(current && placedSource ? [extraButton("Advanced device settings",() =>
                editObject("sources",placedSource.id))] : []),
            ...(current && draft.kind === "emitters" && find("emitters",draft.equipment)
                ? [extraButton("Advanced device settings",() => editObject("emitters",draft.equipment))] : []),
            ...(current && draft.kind === "circuits" && find("circuits",draft.equipment)
                ? [extraButton("Advanced circuit settings",() => editObject("circuits",draft.equipment))] : []),
            ...(current ? [extraButton("Remove from plan",() => {
            if (Object.values(planData().pipes).some(pipe => [pipe.first,pipe.last].includes(nodeId))) {
                $("plan-dialog-error").textContent = "Remove the connected pipes first."; return;
            }
            delete planData().nodes[nodeId]; $("plan-dialog").close(); redrawForms();
        })] : [])
        ]);
    }
    show();
}

function newPipe(first,last,circuit,run,settings={}) {
    const pipes = planData().pipes;
    const existing = Object.values(pipes).some(pipe => pipe.circuit === circuit && pipe.run === run &&
        ((pipe.first === first && pipe.last === last) || (pipe.last === first && pipe.first === last)));
    if (existing) return;
    const name = unique("pipe",Object.keys(pipes));
    pipes[name] = {mounting:"free_running",room:"",wall:"",offset_mm:40,diameter_mm:18,
        wall_thickness_mm:2.5,material:"steel",insulation_mm:9,loss_w_mk:0,...settings,first,last,circuit,run};
    return name;
}
function drawPipe(point,nodeId) {
    const circuit = $("pipe-circuit").value;
    if (!circuit) { planMessage("Add a water circuit before drawing pipes.",true); return; }
    const plan = planData();
    if (!routePoint || !plan.nodes[routePoint]) {
        routePoint = nodeId || addPoint(point);
        planMessage("Choose the next point or an equipment connection. Change floors to draw a riser.");
    } else {
        const first = plan.nodes[routePoint];
        const last = nodeId ? plan.nodes[nodeId] : point;
        if (routePoint === nodeId || (first.x === last.x && first.y === last.y &&
            first.level === (last.level || planFloor) && first.height_mm === (last.height_mm ?? 150))) return;
        const destination = nodeId || addPoint(point);
        const runs = $("pipe-run").value === "pair" ? ["supply","return"] : [$("pipe-run").value];
        const room = first.level === planFloor ? containingRoom({x:(first.x+last.x)/2,y:(first.y+last.y)/2}) : "";
        for (const run of runs) newPipe(routePoint,destination,circuit,run,{room});
        routePoint = destination;
        planMessage("Segment added. Click another point to continue or select End route.");
    }
    drawPlan();
}
function pairedPipes(id) {
    const pipes = planData().pipes, current = pipes[id];
    return Object.keys(pipes).filter(key => {
        const pipe = pipes[key];
        return pipe.circuit === current.circuit && [pipe.first,pipe.last].includes(current.first) &&
            [pipe.first,pipe.last].includes(current.last);
    });
}
function splitPipe(id,point) {
    const plan = planData(), pipe = plan.pipes[id];
    const first = plan.nodes[pipe.first], last = plan.nodes[pipe.last];
    const dx=last.x-first.x, dy=last.y-first.y;
    const span=dx*dx+dy*dy;
    if (!span || first.level !== last.level) { planMessage("Add junctions on level pipe runs.",true); return; }
    const fraction=((point.x-first.x)*dx+(point.y-first.y)*dy)/span;
    if (!(fraction > .02 && fraction < .98)) { planMessage("Click further from an existing endpoint.",true); return; }
    const nodeId = addPoint({x:first.x+fraction*dx,y:first.y+fraction*dy},{
        height_mm:(first.height_mm ?? 150)+fraction*((last.height_mm ?? 150)-(first.height_mm ?? 150))
    });
    for (const name of pairedPipes(id)) {
        const segment = plan.pipes[name], end = segment.last;
        segment.last = nodeId;
        newPipe(nodeId,end,segment.circuit,segment.run,segment);
    }
    planMessage("Junction added. Select Draw pipe and click it to branch.");
    drawPlan();
}
function junctionDialog(id) {
    const point = planData().nodes[id], draft = {...point};
    dialog("Pipe junction",[
        input("Height above floor (mm)",draft.height_mm ?? 150,next => draft.height_mm=next),
        el("small","Drag the junction to move connected pipe segments.")
    ],() => {
        if (!Number.isFinite(draft.height_mm) || draft.height_mm < -2000 || draft.height_mm > 10000) {
            return "Enter a valid junction height.";
        }
        point.height_mm=draft.height_mm;
    },[extraButton("Remove junction",() => {
        if (Object.values(planData().pipes).some(pipe => [pipe.first,pipe.last].includes(id))) {
            $("plan-dialog-error").textContent = "Remove the connected pipes first.";
            return;
        }
        delete planData().nodes[id]; $("plan-dialog").close(); drawPlan();
    })]);
}
function pipeDialog(name) {
    const plan = planData(), current = plan.pipes[name];
    const draft = {mounting:"free_running",room:"",wall:"",offset_mm:40,diameter_mm:18,
        wall_thickness_mm:2.5,material:"steel",insulation_mm:9,loss_w_mk:0,...current};
    const start = {...plan.nodes[current.first]}, end = {...plan.nodes[current.last]};
    const rooms = roomMap(project);
    const onFloor = Object.keys(plan.rooms).filter(id => rooms[id]?.level === start.level);
    let pair = false;
    const bulk = selectedPipes.has(name) ? [...selectedPipes] : [name];
    function show() {
        const fields = [
            input("Mounting",draft.mounting ?? "free_running",next => {
                draft.mounting=next;
                if (!["on_wall","in_wall"].includes(next)) draft.wall="";
            },["free_running","on_wall","in_wall","underfloor","service_void"],false,show),
            input("Surrounding room",draft.room || "",next => { draft.room=next; draft.wall=""; },
                ["",...onFloor],false,show)
        ];
        if (["on_wall","in_wall"].includes(draft.mounting)) {
            fields.push(input("Reference wall",draft.wall || "",next => draft.wall=next,["",...sides]),
                input(draft.mounting === "in_wall" ? "Depth into wall (mm)" : "Distance from wall (mm)",
                    draft.offset_mm ?? 40,next => draft.offset_mm=next));
        }
        if (bulk.length === 1) fields.push(
            input(`Start height above ${start.level} floor (mm)`,start.height_mm ?? 150,next => start.height_mm=next),
            input(`End height above ${end.level} floor (mm)`,end.height_mm ?? 150,next => end.height_mm=next),
            el("small","Heights are measured to the pipe centreline and also apply to other segments connected at these endpoints."));
        fields.push(input("Pipe material",draft.material ?? "steel",next => draft.material=next,["steel","copper","pex","ppr"]),
            input("Outside diameter (mm)",draft.diameter_mm ?? 18,next => draft.diameter_mm=next),
            input("Wall thickness (mm)",draft.wall_thickness_mm ?? 2.5,next => draft.wall_thickness_mm=next),
            input("Insulation thickness (mm)",draft.insulation_mm ?? 9,next => draft.insulation_mm=next),
            input("Heat loss coefficient (W/m·K, 0 uses estimate)",draft.loss_w_mk ?? 0,next => draft.loss_w_mk=next));
        if (pairedPipes(name).length > 1) fields.push(input("Apply to supply-and-return pair",pair,next => pair=next));
        dialog(bulk.length > 1 ? `Pipe settings · ${bulk.length} segments` : `Pipe settings · ${name} (${current.run})`,
            fields,() => {
                const wallMounted = ["on_wall","in_wall"].includes(draft.mounting);
                if (wallMounted && (!draft.room || !draft.wall)) return "Choose a room and reference wall.";
                const within = (value,low,high) => Number.isFinite(value) && value >= low && value <= high;
                if (!within(start.height_mm ?? 150,-2000,10000) || !within(end.height_mm ?? 150,-2000,10000) ||
                    !within(draft.diameter_mm,4,250) || !within(draft.wall_thickness_mm,0.2,15) ||
                    draft.diameter_mm <= 2*draft.wall_thickness_mm || !within(draft.insulation_mm,0,200) ||
                    !within(draft.offset_mm,0,1000) || !within(draft.loss_w_mk,0,100)) {
                    return "Enter valid pipe dimensions and endpoint heights.";
                }
                const targets = new Set(bulk.flatMap(id => pair ? pairedPipes(id) : [id]));
                if (draft.room && [...targets].some(id => {
                    const item = plan.pipes[id], level = rooms[draft.room]?.level;
                    return plan.nodes[item.first].level !== level ||
                        (wallMounted && plan.nodes[item.last].level !== level);
                })) return "Choose a room on the same floor as the segments. Use free-running for risers.";
                for (const id of targets) {
                    for (const key of ["mounting","room","wall","offset_mm","diameter_mm","wall_thickness_mm","material","insulation_mm","loss_w_mk"]) {
                        plan.pipes[id][key]=draft[key];
                    }
                }
                if (bulk.length === 1) {
                    plan.nodes[current.first].height_mm=start.height_mm ?? 150;
                    plan.nodes[current.last].height_mm=end.height_mm ?? 150;
                }
                selectedPipes.clear();
            },[extraButton("Remove segment",() => {
                for (const id of bulk) delete plan.pipes[id];
                selectedPipes.clear(); $("plan-dialog").close(); drawPlan();
            }),extraButton("Configure water circuit",() => editObject("circuits",current.circuit))]);
    }
    show();
}
const editor = $("plan-svg");
editor.addEventListener("pointerdown",event => {
    if (!project || !planFloor || event.button !== 0) return;
    const point=mousePoint(event), target=event.target.dataset;
    if (activeTool === "pipe") { drawPipe(point,target.node); return; }
    if (activeTool === "junction") { if (target.pipe) splitPipe(target.pipe,point); return; }
    if (activeTool === "equipment") { equipmentDialog(point); return; }
    if (activeTool === "wall") { if (target.wall) wallDialog(target.room,target.wall); return; }
    if (activeTool === "select" && target.pipe) {
        if (event.shiftKey) {
            if (selectedPipes.has(target.pipe)) selectedPipes.delete(target.pipe);
            else selectedPipes.add(target.pipe);
            planMessage(`${selectedPipes.size} segments selected. Click a selected segment to edit together.`);
            drawPlan();
        } else {
            if (!selectedPipes.has(target.pipe)) selectedPipes.clear();
            pipeDialog(target.pipe);
        }
        return;
    }
    if (activeTool === "select" && target.node) {
        const origin = planData().nodes[target.node];
        drag={point,node:target.node,origin,position:origin,preview:null};
        editor.setPointerCapture(event.pointerId);
        return;
    }
    if (activeTool === "room" || (activeTool === "select" && target.room)) {
        const name = activeTool === "select" ? target.room : null;
        const origin = name ? planData().rooms[name] : point;
        drag={point,name,origin,position:origin,preview:null};
        editor.setPointerCapture(event.pointerId);
    }
});
editor.addEventListener("pointermove",event => {
    if (!drag) return;
    const point = mousePoint(event);
    if (drag.name || drag.node) {
        drag.position={x:drag.origin.x+point.x-drag.point.x,y:drag.origin.y+point.y-drag.point.y};
    } else {
        drag.preview={x:Math.min(point.x,drag.point.x),y:Math.min(point.y,drag.point.y),
            width:Math.abs(point.x-drag.point.x),height:Math.abs(point.y-drag.point.y)};
    }
    drawPlan();
});
editor.addEventListener("pointerup",() => {
    if (!drag) return;
    const result=drag; drag=null;
    if (result.node) {
        const node = planData().nodes[result.node];
        if (node.x !== result.position.x || node.y !== result.position.y) Object.assign(node,result.position);
        else if (node.kind === "junction") junctionDialog(result.node);
        else equipmentDialog(node,result.node);
    } else if (result.name) {
        const dx = result.position.x-result.origin.x, dy=result.position.y-result.origin.y;
        if (!dx && !dy) roomDialog(result.name);
        else {
            const room = roomMap(project)[result.name];
            for (const point of Object.values(planData().nodes)) {
                if (point.level === planFloor && point.x >= result.origin.x && point.y >= result.origin.y &&
                    point.x <= result.origin.x+room.length_m && point.y <= result.origin.y+room.width_m) {
                    point.x += dx; point.y += dy;
                }
            }
            Object.assign(planData().rooms[result.name],result.position);
        }
    } else if (result.preview?.width >= .25 && result.preview.height >= .25) roomDialog(null,result.preview);
    drawPlan();
});
editor.addEventListener("pointercancel",() => { drag=null; drawPlan(); });
selectTool("select");

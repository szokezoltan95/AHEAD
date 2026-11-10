"use strict";

const liveReadingKeys = {
    heat_exchangers:"heat_kw", rooms:"air_c", circuits:"supply_c", emitters:"total_heat_kw",
    sources:"heat_kw", outdoor_units:"electrical_kw", surfaces:"irradiance_kw_m2",
    pipes:"flow_l_min", solar:"electrical_kw",
};
const equipmentGroups = {
    sources:"Heat sources", outdoor_units:"Outdoor units", circuits:"Water circuits",
    emitters:"Radiators and floor heating", heat_exchangers:"Heat exchangers", solar:"Solar",
};
const readingUnits = {rooms:"°C", circuits:"°C", surfaces:"kW/m²", pipes:"L/min"};
let liveState, liveFloor = "", liveFloorsKey = "", liveGeometryKey = "", inventoryKey = "";
let livePlanItems = [], liveEquipmentCards = [];
let heatmapEnabled = true;
let airCurrentsEnabled = false;
let liveAirSliceM = 0.8;
let liveFitBox = null, liveViewBox = null;

function withUnit(value,unit) {
    return Number.isFinite(value) ? `${format(value)} ${unit}` : "—";
}
function liveReading(group,item) {
    const value = item[liveReadingKeys[group]] ?? (group === "solar" ? item.delivered_kw : null);
    return withUnit(value,readingUnits[group] || "kW");
}

function equipmentStatus(group,item) {
    if (group === "sources") {
        if (item.status === "unavailable") return ["off","Unavailable"];
        if (item.heat_kw > .005) return ["heating","Heating"];
        if (item.heat_kw < -.005) return ["cooling",item.thermostat?.mode === "dry" ? "Drying" : "Cooling"];
        if (item.fan_kw > .0005) return ["flowing","Fan running"];
        if (item.request) return ["waiting",human(item.status || "requested")];
        return ["idle","Idle"];
    }
    if (group === "outdoor_units") {
        if (item.defrost) return ["waiting","Defrost"];
        if (item.electrical_kw > .005) return [item.mode === "cooling" ? "cooling" : "heating",human(item.mode)];
    }
    if (group === "emitters" || group === "circuits") {
        if (item.flow_l_min > .005) return ["flowing","Flowing"];
        return item.thermostat?.request > 0 ? ["waiting","Calling for heat"] : ["idle","No flow"];
    }
    if (group === "heat_exchangers" || group === "solar") {
        const output = group === "solar" ? (item.electrical_kw ?? item.delivered_kw) : item.heat_kw;
        if (output > .005) return ["heating",group === "solar" ? "Producing" : "Transferring heat"];
    }
    return ["idle","Idle"];
}

function selectLive(group,name,scroll=true) {
    selected = group ? `${group}/${name}` : "";
    trendPath = "";
    render(liveState);
    if (scroll) $("live-inspector").scrollIntoView({block:"nearest"});
}

function livePlanItem(group,name,className,...parts) {
    const item = svg("g",{class:className,tabindex:"0",role:"button",
        "data-live-item":`${group}/${name}`});
    const title = svg("title");
    item.append(title,...parts);
    item.onclick = () => selectLive(group,name);
    item.onkeydown = event => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); item.onclick(); }
    };
    return item;
}

function updateLivePlanItem(node,state) {
    const id = node.dataset.liveItem;
    const [group,name] = id.split("/");
    const item = state[group]?.[name];
    const isSelected = id === selected;
    const title = node.firstElementChild;
    const reading = item ? liveReading(group,item) : "—";
    let details = reading;
    node.classList.toggle("selected",isSelected);
    node.setAttribute("aria-pressed",isSelected);

    if (group === "rooms") {
        const temperature = node.querySelector(".live-room-temperature");
        temperature.textContent = reading;
        const load = (item?.emitters_kw ?? 0) + (item?.emitter_radiant_kw ?? 0) + (item?.direct_heat_kw ?? 0);
        node.dataset.state = load > .03 ? "heating" : load < -.03 ? "cooling" : "idle";
        if (Number.isFinite(item?.target_c) && state.configured_objects?.rooms?.[name]?.preset !== "unheated") {
            details += ` · Target ${withUnit(item.target_c,"°C")}`;
        }
    } else if (group === "pipes") {
        const route = state.floorplan.pipes[name];
        const inlet = withUnit(item?.inlet_c,"°C"), outlet = withUnit(item?.outlet_c,"°C");
        node.querySelector(".live-pipe-label").textContent = reading;
        node.dataset.state = !item ? "idle" : item.flow_l_min > .005 ? "flowing" : "idle";
        details = `${human(route.run)} · ${reading} · ${inlet} → ${outlet}`;
        if (item) details += ` · Heat loss ${withUnit(item.heat_loss_kw,"kW")}`;
    } else {
        const [status,label] = item ? equipmentStatus(group,item) : ["idle","Waiting"];
        node.dataset.state = status;
        if (group === "emitters") node.classList.toggle("is-heating", (item?.total_heat_kw || 0) > .03);
        node.querySelector(".live-device-reading").textContent = reading;
        details = `${label} · ${reading}`;
    }
    title.textContent = `${human(name)} · ${details}`;
    node.setAttribute("aria-label",title.textContent);
}

function drawLivePlan(state) {
    liveState = state;
    const plan = state.floorplan, rooms = state.configured_objects?.rooms || {};
    const floors = [...new Set([
        ...Object.keys(plan?.rooms || {}).map(name => rooms[name]?.level),
        ...Object.values(plan?.nodes || {}).map(node => node.level),
    ].filter(Boolean))];
    const hasPlan = floors.length > 0;
    $("live-plan").hidden = !hasPlan;
    $("live-floor-control").hidden = !hasPlan;
    $("live-plan-empty").hidden = hasPlan;
    if (!hasPlan) {
        liveGeometryKey = "";
        liveFitBox = liveViewBox = null;
        livePlanItems = [];
        $("live-svg").replaceChildren();
        $("live-plan-empty").textContent = state.elapsed_s === undefined
            ? human(state.status) + "."
            : "This run has no drawn floor plan. Apply a layout in Configure to see the rooms and pipes here. Live equipment readings are available alongside.";
        return;
    }
    if (!floors.includes(liveFloor)) liveFloor = floors[0];
    const floorsKey = JSON.stringify(floors);
    if (floorsKey !== liveFloorsKey) {
        liveFloorsKey = floorsKey;
        optionList($("live-level"),floors,liveFloor);
    }
    $("live-level").value = liveFloor;
    const sources = state.configured_objects?.sources || {};
    const geometry = JSON.stringify([liveFloor,plan,rooms,sources]);
    if (geometry !== liveGeometryKey) {
        liveGeometryKey = geometry;
        const canvas = $("live-svg");
        liveFitBox = fitBox(plan,rooms,liveFloor);
        liveViewBox = liveFitBox.slice();
        paint(canvas,plan,rooms,liveFloor,liveViewBox,sources);
        livePlanItems = Array.from(canvas.querySelectorAll("[data-live-item]"));
    }
    for (const node of livePlanItems) updateLivePlanItem(node,state);
    drawHeatAirCells(state, plan, rooms, liveFloor);
    drawAirCurrentArrows(state, plan, rooms, liveFloor);
    if ($("live-air-3d-dialog").open) drawAirCurrent3d(state);
    const hasPipes = livePlanItems.some(node => node.classList.contains("live-pipe-item"));
    $("live-pipe-legend").hidden = !hasPipes || $("live-pipes-toggle").getAttribute("aria-pressed") !== "true";
    $("live-pipes-toggle").disabled = !hasPipes;
    $("live-flow-toggle").disabled = !hasPipes || $("live-pipes-toggle").getAttribute("aria-pressed") !== "true";
    $("live-equipment-toggle").disabled = !livePlanItems.some(node => node.classList.contains("live-device-item"));
}

$("live-level").onchange = () => {
    liveFloor = $("live-level").value;
    drawLivePlan(liveState);
};
for (const [id,className] of [
    ["live-pipes-toggle","hide-live-pipes"],["live-equipment-toggle","hide-live-equipment"],
    ["live-flow-toggle","show-live-flows"],
]) {
    const button = $(id);
    button.onclick = () => {
        const pressed = button.getAttribute("aria-pressed") !== "true";
        button.setAttribute("aria-pressed",pressed);
        $("live-svg").classList.toggle(className,id === "live-flow-toggle" ? pressed : !pressed);
        drawLivePlan(liveState);
    };
}
$("live-heatmap-toggle").onclick = () => {
    heatmapEnabled = !heatmapEnabled;
    $("live-heatmap-toggle").setAttribute("aria-pressed",heatmapEnabled);
    drawLivePlan(liveState);
};
$("live-air-slice").oninput = event => {
    liveAirSliceM = Number(event.target.value);
    $("live-air-slice-value").textContent = `${liveAirSliceM.toFixed(1)} m`;
    drawLivePlan(liveState);
};
$("live-currents-toggle").onclick = () => {
    airCurrentsEnabled = !airCurrentsEnabled;
    $("live-currents-toggle").setAttribute("aria-pressed",airCurrentsEnabled);
    $("air-current-legend").hidden = !airCurrentsEnabled;
    drawLivePlan(liveState);
};
let liveAir3dRoom = "";
$("live-air-3d-open").onclick = () => {
    if (!liveState?.floorplan) return;
    updateAir3dRoomOptions(liveState);
    $("live-air-3d-dialog").showModal();
    drawAirCurrent3d(liveState);
};
$("live-air-3d-close").onclick = () => $("live-air-3d-dialog").close();
$("live-air-3d-room").onchange = event => {
    liveAir3dRoom = event.target.value;
    drawAirCurrent3d(liveState);
};
function zoomLivePlan(factor,anchorX=.5,anchorY=.5) {
    const canvas = $("live-svg");
    if (!liveViewBox || !liveFitBox) return;
    const [x,y,width,height] = liveViewBox;
    const nextWidth = Math.max(liveFitBox[2]*.18,Math.min(liveFitBox[2]*5,width*factor));
    const nextHeight = Math.max(liveFitBox[3]*.18,Math.min(liveFitBox[3]*5,height*factor));
    const pointX = x + width*anchorX, pointY = y + height*anchorY;
    liveViewBox = [pointX-anchorX*nextWidth,pointY-anchorY*nextHeight,nextWidth,nextHeight];
    canvas.setAttribute("viewBox",liveViewBox.join(" "));
}
$("live-zoom-in").onclick = () => zoomLivePlan(.8);
$("live-zoom-out").onclick = () => zoomLivePlan(1.25);
$("live-fit-plan").onclick = () => {
    if (!liveFitBox) return;
    liveViewBox = liveFitBox.slice();
    $("live-svg").setAttribute("viewBox",liveViewBox.join(" "));
};
$("live-svg").addEventListener("wheel",event => {
    if ($("live-floor-view").hidden || !liveViewBox) return;
    event.preventDefault();
    const bounds = $("live-svg").getBoundingClientRect();
    zoomLivePlan(event.deltaY < 0 ? .88 : 1.14,
        Math.max(0,Math.min(1,(event.clientX-bounds.left)/bounds.width)),
        Math.max(0,Math.min(1,(event.clientY-bounds.top)/bounds.height)));
},{passive:false});
$("live-overview").onclick = () => selectLive("","",false);
$("live-json").ontoggle = () => {
    if ($("live-json").open && liveState) $("full").textContent = JSON.stringify(liveState,null,2);
};

function metric(iconName,label) {
    const card = el("div"); card.className = "live-metric";
    const name = el("span",label), value = el("strong","—");
    card.append(icon(iconName),name,value);
    $("live-metrics").append(card);
    return value;
}
const metricValues = [
    metric("thermometer","Outdoor"), metric("house","Mean room temperature"),
    metric("boiler","Heating output"), metric("power","Electric load"),
];
const meterMetrics = [
    metric("power","House meter"), metric("power","Heat-pump meter"),
];

function equipmentIcon(group,item,config) {
    if (group === "sources" && (item.outdoor_unit || config?.preset === "air_to_water")) return "fan";
    if (group === "circuits") return "tank";
    if (group === "heat_exchangers") return "exchange";
    return sectionIcons[group];
}

function rebuildInventory(state) {
    const rack = $("live-equipment-list");
    rack.replaceChildren();
    liveEquipmentCards = [];
    for (const [group,label] of Object.entries(equipmentGroups)) {
        const entries = Object.entries(state[group] || {});
        if (!entries.length) continue;
        const section = el("section"); section.className = "live-equipment-group";
        section.append(el("h3",label));
        for (const [name,item] of entries) {
            const config = state.configured_objects?.[group]?.[name];
            const card = el("button"); card.className = "live-equipment-card";
            card.onclick = () => selectLive(group,name);
            const label = el("span"); label.className = "live-equipment-label";
            const where = el("small");
            label.append(el("strong",human(name)),where);
            const values = el("span"); values.className = "live-equipment-values";
            const reading = el("strong"), status = el("small");
            status.className = "live-device-status";
            values.append(reading,status);
            card.append(icon(equipmentIcon(group,item,config)),label,values);
            section.append(card);
            liveEquipmentCards.push({group,name,card,where,reading,status});
        }
        rack.append(section);
    }
    if (!liveEquipmentCards.length) {
        const empty = el("p","Waiting for equipment readings."); empty.className = "live-empty"; rack.append(empty);
    }
}

function drawLiveDashboard(state) {
    const temperatures = Object.values(state.rooms || {}).map(room => room.air_c).filter(Number.isFinite);
    const mean = temperatures.length ? temperatures.reduce((sum,value) => sum+value,0)/temperatures.length : null;
    const sources = Object.values(state.sources || {});
    const heat = sources.length ? sources.reduce((sum,item) => sum+Math.max(0,item.heat_kw || 0),0) : null;
    const values = [
        withUnit(state.weather?.actual_outdoor_c,"°C"), withUnit(mean,"°C"),
        withUnit(heat,"kW"), withUnit(state.balance?.electricity_kw,"kW"),
    ];
    metricValues.forEach((node,index) => { node.textContent = values[index]; });
    const separateMeter = state.weather?.heat_pump_price != null ||
        sources.some(item => item.heat_pump_tariff);
    const meterLoads = [state.balance?.house_electricity_kw, state.balance?.heat_pump_electricity_kw];
    meterMetrics.forEach((node,index) => {
        node.parentElement.hidden = !separateMeter;
        node.textContent = withUnit(meterLoads[index],"kW");
    });

    const key = JSON.stringify(Object.keys(equipmentGroups).map(group =>
        Object.keys(state[group] || {})));
    if (key !== inventoryKey) { inventoryKey = key; rebuildInventory(state); }
    const placement = new Map();
    for (const node of Object.values(state.floorplan?.nodes || {})) {
        if (node.equipment) placement.set(`${node.kind}/${node.equipment}`,node.level);
    }
    $("live-equipment-count").textContent = liveEquipmentCards.length;
    for (const {group,name,card,where,reading,status} of liveEquipmentCards) {
        const item = state[group][name], id = `${group}/${name}`;
        const floor = placement.get(id), room = item.zone;
        where.textContent = [room && human(room), floor ? human(floor) : "Unplaced"].filter(Boolean).join(" · ");
        card.setAttribute("aria-pressed",selected === id);
        const [mode,label] = equipmentStatus(group,item);
        status.textContent = label;
        status.dataset.state = mode;
        reading.textContent = liveReading(group,item);
    }
}

let activeRemote = "";
const remoteInputs = new Map();

function drawDeviceRemote(state) {
    const panel = $("live-remote"), controls = $("live-remote-fields"), status = $("live-remote-status");
    const [device, descriptor] = Object.entries(state.remote_devices || {}).find(([, item]) =>
        selected === `${item.group}/${item.device}`) || [];
    panel.hidden = !descriptor;
    const layout = descriptor ? JSON.stringify([device, descriptor.fields]) : "";
    if (activeRemote !== layout) {
        activeRemote = layout;
        controls.replaceChildren();
        remoteInputs.clear();
        status.hidden = true;
        for (const [field, spec] of Object.entries(descriptor?.fields || {})) {
            const label = el("label");
            const title = el("span", spec.unit ? `${spec.label} (${spec.unit})` : spec.label);
            title.id = `remote-label-${field}`;
            label.append(title);
            const input = el(spec.options ? "select" : "input");
            input.setAttribute("aria-labelledby", title.id);
            if (spec.options) {
                for (const option of spec.options) {
                    const node = el("option", human(option));
                    node.value = option;
                    input.append(node);
                }
            } else {
                Object.assign(input, {type: "number", min: spec.min, max: spec.max, step: spec.step});
            }
            label.append(input);
            controls.append(label);
            const control = {input, pending: false, requestedAt: -1};
            remoteInputs.set(field, control);
            input.onchange = async () => {
                if (!input.value || !input.reportValidity()) return;
                const value = spec.options ? input.value : input.valueAsNumber;
                control.pending = true;
                input.disabled = true;
                status.hidden = false;
                status.textContent = "Sending…";
                try {
                    await api("/api/remote", {device, field, value});
                    control.requestedAt = liveState.elapsed_s;
                    if (activeRemote === layout) status.textContent = `${spec.label} updated.`;
                } catch (error) {
                    if (activeRemote === layout) status.textContent = error.message;
                } finally {
                    input.disabled = false;
                    control.pending = false;
                }
            };
        }
    }
    const values = state.remote_state?.[device] || {};
    for (const [field, control] of remoteInputs) {
        if (!control.pending && state.elapsed_s > control.requestedAt && document.activeElement !== control.input) {
            control.input.value = values[field] ?? "";
        }
    }
}

function drawHeatAirCells(state, plan, rooms, floor) {
    const svgCanvas = $("live-svg");
    svgCanvas.querySelectorAll(".air-cell, .heat-arrow").forEach(node=>node.remove());
    if (!heatmapEnabled) return;
    const layer = svg("g",{class:"heat-overlay"});
    const levels=state.configured_objects?.levels||{};
    const floorRooms=Object.entries(plan.rooms||{}).filter(([name,position])=>
        rooms[name]?.level===floor);
    const maxHeight=Math.max(0.5,...floorRooms.map(([name])=>
        rooms[name]?.height_m||levels[rooms[name]?.level]?.height_m||2.7));
    const sliceInput=$("live-air-slice");
    sliceInput.max=maxHeight.toFixed(1);
    liveAirSliceM=Math.min(liveAirSliceM,maxHeight);
    sliceInput.value=liveAirSliceM;
    $("live-air-slice-value").textContent=`${liveAirSliceM.toFixed(1)} m`;
    for (const [name,position] of Object.entries(plan.rooms||{})) {
        const room=rooms[name], reading=state.rooms?.[name];
        if(!room||room.level!==floor||!reading)continue;
        const heating=(reading.emitters_kw||0)+(reading.emitter_radiant_kw||0)+(reading.direct_heat_kw||0);
        const [nx,ny]=reading.air_grid?.shape||[7,7];
        const cellW=room.length_m/nx,cellH=room.width_m/ny;
        const heights=reading.air_grid?.z_centers_m||[0.8];
        const iz=heights.reduce((best,height,index)=>
            Math.abs(height-liveAirSliceM)<Math.abs(heights[best]-liveAirSliceM)?index:best,0);
        const slice=reading.air_cells_c?.[iz]||reading.air_cells_c?.[0]||[];
        const gx=position.x+room.length_m*.5,gy=position.y+room.width_m*.5;
        for(let y=0;y<ny;y++)for(let x=0;x<nx;x++){
            const cell=svg("rect",{x:(position.x+x*cellW)*PLAN_SCALE,y:(position.y+y*cellH)*PLAN_SCALE,
                width:cellW*PLAN_SCALE,height:cellH*PLAN_SCALE,class:"air-cell"});
            const temp=slice?.[y]?.[x] ?? reading.air_c;
            cell.style.fill=`hsl(${Math.max(0,210-(temp-16)*13)} 78% 58% / .28)`;
            const tooltip=svg("title");tooltip.textContent=`${name.replaceAll("_"," ")} · ${temp.toFixed(1)} °C`;
            cell.append(tooltip);
            layer.append(cell);
        }
        if(heating>0.03){
            const arrow=svg("text",{x:gx*PLAN_SCALE,y:gy*PLAN_SCALE,class:"heat-arrow"});
            arrow.textContent="↑";layer.append(arrow);
        }
    }
    for (const surface of Object.values(state.surfaces||{})) {
        if (surface.boundary!=="outdoors"||(surface.heat_into_room_kw||0)>=-.01)continue;
        const room=rooms[surface.room],position=plan.rooms?.[surface.room];
        if(!room||!position||room.level!==floor)continue;
        const az=(surface.azimuth_deg||0)%360;
        let x,y,arrow;
        if(surface.tilt_deg<45){x=position.x+room.length_m/2;y=position.y+room.width_m/2;arrow="↑";}
        else if(az<45||az>=315){x=position.x+room.length_m/2;y=position.y+.16;arrow="↑";}
        else if(az<135){x=position.x+room.length_m-.16;y=position.y+room.width_m/2;arrow="→";}
        else if(az<225){x=position.x+room.length_m/2;y=position.y+room.width_m-.16;arrow="↓";}
        else{x=position.x+.16;y=position.y+room.width_m/2;arrow="←";}
        const loss=svg("text",{x:x*PLAN_SCALE,y:y*PLAN_SCALE,class:"heat-arrow exterior-arrow"});
        loss.textContent=arrow;layer.append(loss);
    }
    for(const link of state.heat_links||[]){
        if(!["airflow","radiation","conduction"].includes(link.kind)||Math.abs(link.kw)<.005)continue;
        const a=plan.rooms?.[link.first],b=plan.rooms?.[link.second];
        if(!a||!b||rooms[link.first]?.level!==floor||rooms[link.second]?.level!==floor)continue;
        const ar=rooms[link.first],br=rooms[link.second];
        const ax=(a.x+ar.length_m/2)*PLAN_SCALE, ay=(a.y+ar.width_m/2)*PLAN_SCALE;
        const bx=(b.x+br.length_m/2)*PLAN_SCALE, by=(b.y+br.width_m/2)*PLAN_SCALE;
        const forward=link.kw>0;
        layer.append(svg("line",{x1:forward?ax:bx,y1:forward?ay:by,x2:forward?bx:ax,y2:forward?by:ay,
            class:`heat-arrow interroom-arrow ${link.kind}-arrow`, "marker-end":"url(#heat-arrowhead)"}));
    }
    const defs=svgCanvas.querySelector("defs");
    if(defs&&!defs.querySelector("#heat-arrowhead")){
        const marker=svg("marker",{id:"heat-arrowhead",viewBox:"0 0 10 10",refX:8,refY:5,
            markerWidth:5,markerHeight:5,orient:"auto-start-reverse"});
        marker.append(svg("path",{d:"M 0 0 L 10 5 L 0 10 z",fill:"#9d55c7"}));defs.append(marker);
    }
    const firstItem=svgCanvas.querySelector(".live-pipe-item, .live-device-item");
    svgCanvas.insertBefore(layer,firstItem||null);
}

function drawAirCurrentArrows(state, plan, rooms, floor) {
    const svgCanvas = $("live-svg");
    svgCanvas.querySelector(".air-current-layer")?.remove();
    if (!airCurrentsEnabled) return;
    const candidates = [];
    let maximum = 0;
    for (const [name,position] of Object.entries(plan.rooms||{})) {
        const room = rooms[name], field = state.rooms?.[name]?.air_currents;
        if (!room || room.level !== floor || !field) continue;
        const heights = field.z_centers_m || [];
        if (!heights.length) continue;
        const iz = heights.reduce((best,height,index) =>
            Math.abs(height-liveAirSliceM) < Math.abs(heights[best]-liveAirSliceM) ? index : best,0);
        const vectors = field.vectors_m_s?.[iz] || [];
        for (let iy=0;iy<vectors.length;iy++) for (let ix=0;ix<vectors[iy].length;ix++) {
            // Serialized vectors use grid-axis order [vertical, room-y, room-x].
            const [vz,vy,vx] = vectors[iy][ix] || [0,0,0];
            const horizontal = Math.hypot(vx,vy);
            maximum = Math.max(maximum,horizontal,Math.abs(vz));
            candidates.push({name,position,field,ix,iy,vx,vy,vz,horizontal});
        }
    }
    const layer = svg("g",{class:"air-current-layer"});
    const defs = svgCanvas.querySelector("defs");
    if (defs && !defs.querySelector("#air-current-arrowhead")) {
        const marker = svg("marker",{id:"air-current-arrowhead",viewBox:"0 0 10 10",refX:8,refY:5,
            markerWidth:4,markerHeight:4,orient:"auto"});
        marker.append(svg("path",{d:"M 0 0 L 10 5 L 0 10 z",fill:"#54d5e8"}));
        defs.append(marker);
    }
    const threshold = Math.max(.003,maximum*.06);
    for (const item of candidates) {
        const {name,position,field,ix,iy,vx,vy,vz,horizontal} = item;
        const x = (position.x + field.x_centers_m[ix]) * PLAN_SCALE;
        const y = (position.y + field.y_centers_m[iy]) * PLAN_SCALE;
        const strength = Math.hypot(horizontal,vz);
        if (horizontal >= threshold) {
            const ratio = Math.sqrt(horizontal / Math.max(maximum,1e-9));
            const length = 9 + 25*ratio;
            const ux = vx/horizontal, uy = vy/horizontal;
            const arrow = svg("line",{
                x1:x-ux*length/2,y1:y-uy*length/2,
                x2:x+ux*length/2,y2:y+uy*length/2,
                class:"air-current-arrow","marker-end":"url(#air-current-arrowhead)"
            });
            const title = svg("title");
            title.textContent = `${name.replaceAll("_"," ")} · modeled air current · relative strength ${(strength/Math.max(maximum,1e-9)).toFixed(2)}`;
            arrow.append(title); layer.append(arrow);
        }
        if (Math.abs(vz) >= threshold) {
            const verticalX=x+(horizontal>=threshold?8:0),verticalY=y-(horizontal>=threshold?8:0);
            const updraft=vz>0;
            const group=svg("g",{class:`air-current-vertical-group ${updraft?"updraft":"downdraft"}`});
            group.append(svg("circle",{cx:verticalX,cy:verticalY,r:7,class:`air-current-marker ${updraft?"air-current-updraft":"air-current-downdraft"}`}));
            const arrow = svg("text",{x:verticalX,y:verticalY,class:"air-current-vertical"});
            arrow.textContent = updraft ? "↑" : "↓";
            const title = svg("title");
            title.textContent = `${name.replaceAll("_"," ")} · ${updraft?"updraft":"downdraft"} · relative strength ${(Math.abs(vz)/Math.max(maximum,1e-9)).toFixed(2)}`;
            group.append(arrow,title); layer.append(group);
        }
    }
    const firstItem = svgCanvas.querySelector(".live-pipe-item, .live-device-item");
    svgCanvas.insertBefore(layer,firstItem||null);
}

function updateAir3dRoomOptions(state) {
    const select=$("live-air-3d-room"),plan=state.floorplan;
    const available=Object.keys(plan?.rooms||{}).filter(name=>state.rooms?.[name]?.air_currents);
    select.replaceChildren(...available.map(name=>{
        const option=document.createElement("option"); option.value=name; option.textContent=name.replaceAll("_"," "); return option;
    }));
    if (!available.includes(liveAir3dRoom)) liveAir3dRoom=available[0]||"";
    select.value=liveAir3dRoom;
    $("live-air-3d-open").disabled=!available.length;
}

function drawAirCurrent3d(state) {
    const canvas=$("live-air-3d-svg"),plan=state?.floorplan;
    updateAir3dRoomOptions(state||{});
    canvas.setAttribute("viewBox","0 0 1000 630");
    const name=liveAir3dRoom,room=state?.configured_objects?.rooms?.[name];
    const position=plan?.rooms?.[name],reading=state?.rooms?.[name],field=reading?.air_currents;
    canvas.replaceChildren();
    if (!room||!position||!field) return;
    const levels=state.configured_objects?.levels||{};
    const length=room.length_m,width=room.width_m,height=room.height_m||levels[room.level]?.height_m||2.7;
    const vectors=field.vectors_m_s||[],xs=field.x_centers_m||[],ys=field.y_centers_m||[],zs=field.z_centers_m||[];
    const points=[];
    for (const z of [0,height]) for (const x of [0,length]) for (const y of [0,width]) {
        points.push([x-y,(x+y)*.5-z*1.2]);
    }
    const minX=Math.min(...points.map(point=>point[0])),maxX=Math.max(...points.map(point=>point[0]));
    const minY=Math.min(...points.map(point=>point[1])),maxY=Math.max(...points.map(point=>point[1]));
    const scale=Math.min(720/Math.max(maxX-minX,.1),430/Math.max(maxY-minY,.1));
    const project=(x,y,z)=>[500+(x-y-(minX+maxX)/2)*scale,
        315+((x+y)*.5-z*1.2-(minY+maxY)/2)*scale];
    const corners={
        a:project(0,0,0),b:project(length,0,0),c:project(length,width,0),d:project(0,width,0),
        A:project(0,0,height),B:project(length,0,height),C:project(length,width,height),D:project(0,width,height),
    };
    const polygon=(keys,className)=>svg("polygon",{points:keys.map(key=>corners[key].join(",")).join(" "),class:className});
    canvas.append(polygon(["a","b","c","d"],"air-3d-floor"),
        polygon(["a","b","B","A"],"air-3d-wall"),polygon(["b","c","C","B"],"air-3d-wall"));
    const edges=[["a","b"],["b","c"],["c","d"],["d","a"],["A","B"],["B","C"],["C","D"],["D","A"],["a","A"],["b","B"],["c","C"],["d","D"]];
    for (const [first,last] of edges) canvas.append(svg("line",{
        x1:corners[first][0],y1:corners[first][1],x2:corners[last][0],y2:corners[last][1],class:"air-3d-room-edge"}));
    const cellGrid=reading.air_cells_c||[],maxSpeed=Math.max(.001,...vectors.flat(2).map(v=>Math.hypot(v[0],v[1],v[2])));
    const defs=svg("defs"),marker=svg("marker",{id:"air-3d-arrowhead",viewBox:"0 0 10 10",refX:8,refY:5,
        markerWidth:4,markerHeight:4,orient:"auto"});
    marker.append(svg("path",{d:"M 0 0 L 10 5 L 0 10 z",fill:"context-stroke"}));defs.append(marker);canvas.append(defs);
    const threshold=maxSpeed*.06,cellSize=field.cell_size_m||[1,1,.5];
    for (let iz=0;iz<vectors.length;iz++) for (let iy=0;iy<(vectors[iz]||[]).length;iy++) {
        for (let ix=0;ix<(vectors[iz][iy]||[]).length;ix++) {
            // Air-current components are serialized as [vertical, y, x].
            const [vz,vy,vx]=vectors[iz][iy][ix]||[0,0,0],speed=Math.hypot(vx,vy,vz);
            if (speed<threshold) continue;
            const x=xs[ix],y=ys[iy],z=zs[iz];
            const screen=project(x,y,z);
            const gx=Math.max(0,Math.min((cellGrid[iz]?.[0]?.length||1)-1,Math.floor(x/cellSize[0])));
            const gy=Math.max(0,Math.min((cellGrid[iz]?.length||1)-1,Math.floor(y/cellSize[1])));
            const temp=cellGrid[iz]?.[gy]?.[gx]??reading.air_c;
            const color=`hsl(${Math.max(0,210-(temp-16)*13)} 78% 58%)`;
            canvas.append(svg("circle",{cx:screen[0],cy:screen[1],r:4,fill:color,class:"air-3d-cell"}));
            const distance=.18+.48*Math.sqrt(speed/maxSpeed),unit=distance/speed;
            const end=project(x+vx*unit,y+vy*unit,z+vz*unit);
            const vertical=Math.abs(vz)>=Math.hypot(vx,vy)*.7;
            const type=vertical?(vz>0?"updraft":"downdraft"):"horizontal";
            const arrow=svg("line",{x1:screen[0],y1:screen[1],x2:end[0],y2:end[1],
                class:`air-3d-vector ${type}`,"marker-end":"url(#air-3d-arrowhead)"});
            const title=svg("title");title.textContent=`${name.replaceAll("_"," ")} · ${temp.toFixed(1)} °C · x ${vx.toFixed(3)}, y ${vy.toFixed(3)}, z ${vz.toFixed(3)} m/s (modeled)`;
            arrow.append(title);canvas.append(arrow);
        }
    }
    for (const [label,point] of [["X",project(length,0,0)],["Y",project(0,width,0)],["Z",project(0,0,height)]]) {
        const text=svg("text",{x:point[0]+8,y:point[1]-5,class:"air-3d-axis-label"});text.textContent=label;canvas.append(text);
    }
}

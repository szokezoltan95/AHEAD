"use strict";

const liveReadingKeys = {
    heat_exchangers:"heat_kw", rooms:"air_c", circuits:"supply_c", emitters:"room_heat_kw",
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
        const load = (item?.emitters_kw ?? 0) + (item?.direct_heat_kw ?? 0);
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
        paint(canvas,plan,rooms,liveFloor,fitBox(plan,rooms,liveFloor),sources);
        livePlanItems = Array.from(canvas.querySelectorAll("[data-live-item]"));
    }
    for (const node of livePlanItems) updateLivePlanItem(node,state);
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

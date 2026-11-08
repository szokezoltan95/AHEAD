"use strict";

let systemView = "piping", systemCircuit = "", systemCircuitsKey = "", pipingKey = "";
let diagramLines = [], heatRows = [], heatRowsKey = "";
const systemTabs = ["piping","heat","floor"];
for (const button of document.querySelectorAll("[data-live-view]")) {
    button.onclick = () => {
        systemView = button.dataset.liveView;
        for (const view of systemTabs) {
            $(`live-${view}-view`).hidden = view !== systemView;
            document.querySelector(`[data-live-view="${view}"]`).setAttribute("aria-pressed",view === systemView);
        }
        drawNetworkViews(liveState);
    };
}
$("live-circuit").onchange = () => { systemCircuit = $("live-circuit").value; drawNetworkViews(liveState); };
$("heat-filter").onchange = () => drawNetworkViews(liveState);
$("live-fit-circuit").onclick = () => {
    const control = $("live-fit-circuit");
    const pressed = control.getAttribute("aria-pressed") !== "true";
    control.setAttribute("aria-pressed",pressed);
    $("live-piping-diagram").classList.toggle("fit-network",pressed);
};

function circuitGraph(state,circuit) {
    const plan = state.floorplan;
    const drawn = Object.entries(plan?.pipes || {}).filter(([,pipe]) => pipe.circuit === circuit);
    const nodes = new Map(), edges = [];
    if (drawn.length) {
        for (const [name,pipe] of drawn) {
            for (const id of [pipe.first,pipe.last]) {
                const node = plan.nodes[id];
                if (node) nodes.set(id,{group:node.kind,name:node.equipment || id});
            }
            if (nodes.has(pipe.first) && nodes.has(pipe.last)) edges.push({id:name,...pipe});
        }
        return {nodes,edges,inferred:false};
    }
    const root = "manifold";
    nodes.set(root,{group:"circuits",name:circuit});
    const connect = (id, group, name, first, last, emitter) => {
        nodes.set(id,{group,name});
        for (const run of ["supply","return"]) {
            edges.push({id:id+"-"+run,run,first,last,emitter});
        }
    };
    for (const [name,item] of Object.entries(state.sources || {})) {
        if (item.loop === circuit) connect("source/"+name,"sources",name,"source/"+name,root);
    }
    for (const [name,item] of Object.entries(state.emitters || {})) {
        if (item.loop === circuit) connect("emitter/"+name,"emitters",name,root,"emitter/"+name,name);
    }
    return {nodes,edges,inferred:true};
}

function diagramColumns(graph) {
    const {nodes,edges} = graph;
    const connections = new Map([...nodes.keys()].map(key => [key,new Set()]));
    for (const edge of edges) {
        connections.get(edge.first).add(edge.last);
        connections.get(edge.last).add(edge.first);
    }
    const roots = [...nodes].filter(([,node]) => node.group === "sources").map(([key]) => key);
    if (!roots.length) {
        const circuit = [...nodes].find(([,node]) => node.group === "circuits");
        if (circuit) roots.push(circuit[0]);
    }
    const depths = new Map(), columns = [];
    const insert = (key,depth) => {
        depths.set(key,depth);
        (columns[depth] ??= []).push(key);
    };
    for (const key of roots) insert(key,0);
    for (const key of [...roots,...nodes.keys()]) {
        if (!depths.has(key)) insert(key,0);
        const queue = [key];
        for (let index=0;index<queue.length;index++) {
            const current = queue[index];
            for (const next of connections.get(current)) {
                if (depths.has(next)) continue;
                insert(next,depths.get(current)+1);
                queue.push(next);
            }
        }
    }
    return columns;
}

function buildPiping(graph,state) {
    const {nodes,edges,inferred} = graph;
    const columns = diagramColumns(graph), positions = new Map();
    const rows = Math.max(1,...columns.map(column => column.length));
    const height = Math.max(135,rows*79+28);
    let left = 26;
    for (const keys of columns) {
        const half = keys.some(key => nodes.get(key).group !== "junction") ? 58 : 10;
        for (const [index,key] of keys.entries()) {
            positions.set(key,{x:left+half,y:(index+.5)*height/keys.length});
        }
        left += 2*half+45;
    }
    const width = Math.max(350,left);
    const graphic = svg("svg",{class:"live-network",width,height,viewBox:`0 0 ${width} ${height}`,
        role:"group","aria-label":"Water circuit connections"});
    diagramLines = [];
    const tracks = new Map();
    for (const edge of edges) {
        const first = positions.get(edge.first), last = positions.get(edge.last);
        const offset = edge.run === "supply" ? -7 : 7;
        const direction = last.x >= first.x ? 1 : -1;
        const ax = first.x + (nodes.get(edge.first).group === "junction" ? 0 : direction*58);
        const bx = last.x - (nodes.get(edge.last).group === "junction" ? 0 : direction*58);
        const ay = first.y+offset, by = last.y+offset;
        const pair = [edge.first,edge.last].sort().join("/");
        const rank = tracks.get(pair) ?? tracks.size;
        tracks.set(pair,rank);
        const elbow = first.x === last.x ? first.x+70+offset :
            (ax+bx)/2 + (rank%5-2)*4+offset;
        const points = `${ax},${ay} ${elbow},${ay} ${elbow},${by} ${bx},${by}`;
        const path = svg("polyline",{points,class:`plan-pipe ${edge.run}`});
        const target = svg("polyline",{points,class:"pipe-target"});
        const group = inferred ? svg("g",{class:"live-pipe-item"}) :
            livePlanItem("pipes",edge.id,"live-pipe-item",path,target);
        if (inferred) group.append(path);
        const arrow = svg("path",{d:"M-3 -3 3 0-3 3",class:`network-arrow ${edge.run}`});
        const label = svg("text",{x:(elbow+bx)/2,y:by+(offset<0 ? -5 : 12),"text-anchor":"middle",class:"network-flow"});
        group.append(arrow,label);
        graphic.append(group);
        diagramLines.push({edge,group,arrow,label,x:(elbow+bx)/2,y:by,forward:Math.sign(bx-elbow) || direction});
    }
    for (const [key,node] of nodes) {
        const {x,y} = positions.get(key);
        if (node.group === "junction") {
            const junction = svg("g");
            for (const [run,offset] of [["supply",-7],["return",7]]) {
                if (edges.some(edge => edge.run === run && (edge.first === key || edge.last === key))) {
                    junction.append(svg("circle",{cx:x,cy:y+offset,r:3.6,class:`network-junction ${run}`}));
                }
            }
            const title = svg("title"); title.textContent = human(node.name); junction.append(title);
            graphic.append(junction);
            continue;
        }
        const rect = svg("rect",{x:x-58,y:y-20,width:116,height:40,rx:7,class:"network-node"});
        const label = svg("text",{x:x-32,y:y-3,class:"network-title"});
        const reading = svg("text",{x:x-32,y:y+12,class:"network-reading"});
        const item = state[node.group]?.[node.name] || {};
        const config = state.configured_objects?.[node.group]?.[node.name];
        const symbol = icon(equipmentIcon(node.group,item,config));
        for (const [key,value] of Object.entries({x:x-51,y:y-7,width:14,height:14,class:"icon plan-device-icon"})) {
            symbol.setAttribute(key,value);
        }
        const name = human(node.name);
        label.textContent = name.length > 15 ? name.slice(0,14)+"…" : name;
        graphic.append(livePlanItem(node.group,node.name,"network-device",rect,symbol,label,reading));
    }
    $("live-piping-diagram").replaceChildren(graphic);
}

function drawPiping(state) {
    const circuits = Object.keys(state.circuits || {});
    const available = circuits.length > 0;
    $("live-circuit-control").hidden = !available;
    $("live-fit-circuit").hidden = !available;
    $("live-system-legend").hidden = !available;
    if (!available) {
        $("live-piping-description").textContent = state.elapsed_s === undefined ?
            "Waiting for the simulator." : "There are no water circuits in this run.";
        $("live-piping-diagram").replaceChildren();
        pipingKey = "";
        return;
    }
    if (!circuits.includes(systemCircuit)) systemCircuit = circuits[0];
    const circuitsKey = JSON.stringify(circuits);
    if (circuitsKey !== systemCircuitsKey) {
        systemCircuitsKey = circuitsKey;
        optionList($("live-circuit"),circuits,systemCircuit);
    }
    const graph = circuitGraph(state,systemCircuit);
    $("live-piping-description").textContent = graph.inferred ?
        "Connection schematic based on circuit assignments. This circuit has no drawn pipe routes." :
        "Drawn supply and return routes. Dots mark connected junctions; line crossings without a dot are separate.";
    const key = JSON.stringify([systemCircuit,[...graph.nodes],graph.edges,graph.inferred]);
    if (key !== pipingKey) { pipingKey=key; buildPiping(graph,state); }
    for (const {edge,group,arrow,label,x,y,forward} of diagramLines) {
        const pipe = state.pipes?.[edge.id], emitter = state.emitters?.[edge.emitter];
        const flow = pipe?.flow_l_min ?? emitter?.flow_l_min;
        const active = Number.isFinite(flow) && flow > .005;
        group.dataset.state = active ? "flowing" : "idle";
        const pressed = !graph.inferred && selected === "pipes/"+edge.id;
        group.classList.toggle("selected",pressed);
        if (!graph.inferred) {
            const title = `${human(edge.id)} · ${withUnit(flow,"L/min")} · ${withUnit(pipe?.heat_loss_kw,"kW")} heat loss`;
            group.firstElementChild.textContent = title;
            group.setAttribute("aria-label",title);
            group.setAttribute("aria-pressed",pressed);
        }
        const orientation = (graph.inferred ? edge.run === "supply" : pipe?.flow_from === edge.first) ? forward : -forward;
        arrow.setAttribute("transform",`translate(${x} ${y}) scale(${orientation} 1)`);
        arrow.style.display = active && (graph.inferred || pipe?.flow_from) ? "" : "none";
        label.textContent = active ? withUnit(flow,"L/min") : "";
    }
    for (const node of $("live-piping-diagram").querySelectorAll(".network-device")) {
        const [group,name] = node.dataset.liveItem.split("/");
        const item = state[group]?.[name];
        const pressed = selected === node.dataset.liveItem;
        node.classList.toggle("selected",pressed);
        node.setAttribute("aria-pressed",pressed);
        const reading = item ? liveReading(group,item) : "—";
        node.querySelector(".network-reading").textContent = reading;
        node.firstElementChild.textContent = `${human(name)} · ${reading}`;
        node.setAttribute("aria-label",node.firstElementChild.textContent);
    }
}

function thermalLinks(state,filter) {
    const links = [];
    const item = (group,name) => ({group,name});
    const add = (first,second,kind,kw) => {
        if (Number.isFinite(kw)) links.push({first,second,kind,kw});
    };
    if (filter === "interroom") {
        const totals = new Map();
        for (const {first,second,kind,kw} of state.heat_links || []) {
            const rooms = [first,second].sort();
            const key = JSON.stringify([...rooms,kind]);
            totals.set(key,(totals.get(key) || 0) + (first === rooms[0] ? kw : -kw));
        }
        for (const [key,kw] of totals) {
            const [first,second,kind] = JSON.parse(key);
            add(item("rooms",first),item("rooms",second),kind,kw);
        }
    } else if (filter === "environment") {
        for (const [room,reading] of Object.entries(state.rooms || {})) {
            add(item(null,"Outdoors"),item("rooms",room),"conduction",reading.envelope_kw);
            add(item(null,"Outdoor air"),item("rooms",room),"airflow",reading.ventilation_kw);
            if (reading.ground_kw) add(item(null,"Ground"),item("rooms",room),"conduction",reading.ground_kw);
            if (reading.solar_kw) add(item(null,"Sun"),item("rooms",room),"solar",reading.solar_kw);
        }
    } else {
        for (const [group,components] of Object.entries({
            sources:state.sources,emitters:state.emitters,pipes:state.pipes,
        })) for (const [name,value] of Object.entries(components || {})) {
            const room = value.zone || value.room;
            const loop = value.loop && item("circuits",value.loop);
            if (room || loop || group === "pipes") {
                const target = room ? item("rooms",room) : loop || item(null,"Surroundings");
                add(item(group,name),target,"equipment",value.room_heat_kw ?? value.heat_kw ?? value.heat_loss_kw);
            }
        }
        for (const value of Object.values(state.heat_exchangers || {})) {
            add(item("circuits",value.source_circuit),item("circuits",value.circuit),"equipment",value.heat_kw);
        }
        for (const [name,value] of Object.entries(state.solar || {})) {
            if (value.circuit) add(item("solar",name),item("circuits",value.circuit),"solar",value.delivered_kw);
        }
    }
    return links;
}

const heatLabels = {conduction:"Conduction",airflow:"Air exchange",radiation:"Radiation",solar:"Solar gain",equipment:"Equipment"};
function heatEndpoint(endpoint) {
    const label = endpoint.group ? human(endpoint.name) : endpoint.name;
    if (!endpoint.group) return el("span",label);
    const button = el("button",label);
    button.onclick = () => selectLive(endpoint.group,endpoint.name);
    return button;
}

function drawHeat(state) {
    const filter = $("heat-filter").value;
    const links = thermalLinks(state,filter);
    const key = JSON.stringify([filter,links.map(({first,second,kind,kw}) => [first,second,kind,kw < 0])]);
    if (heatRowsKey !== key) {
        heatRowsKey = key;
        const root = $("live-heat-links");
        root.replaceChildren();
        heatRows = links.map(({first,second,kind,kw}) => {
            const [from,to] = kw >= 0 ? [first,second] : [second,first];
            const row = el("div"); row.className="heat-link"; row.dataset.kind=kind;
            const transfer = el("span"); transfer.className="heat-link-transfer";
            const power = el("strong");
            const arrow = svg("svg",{viewBox:"0 0 100 12","aria-hidden":"true"});
            const path = svg("path",{fill:"none",stroke:"currentColor","stroke-width":"2"});
            arrow.append(path); transfer.append(power,arrow);
            row.append(heatEndpoint(from),transfer,heatEndpoint(to)); root.append(row);
            return {row,power,path,from,to,kind};
        });
        if (!links.length) {
            const empty = el("p","There are no heat connections in this category.");
            empty.className="live-empty";
            root.append(empty);
        }
    }
    heatRows.forEach(({row,power,path,from,to,kind},index) => {
        const watts = Math.round(Math.abs(links[index].kw)*1000);
        power.textContent = `${heatLabels[kind]} · ${watts} W`;
        path.setAttribute("d",watts ? "M3 6h94m-7-4 7 4-7 4" : "M3 6h94");
        row.setAttribute("aria-label",`${from.name} ${watts ? "to" : "and"} ${to.name}, ${heatLabels[kind]}, ${watts} watts`);
    });
}

function drawNetworkViews(state) {
    if (!state) return;
    if (systemView === "piping") drawPiping(state);
    if (systemView === "heat") drawHeat(state);
}

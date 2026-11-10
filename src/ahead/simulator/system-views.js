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
for (const input of document.querySelectorAll("[data-pipe-metric]")) {
    input.onchange = () => drawNetworkViews(liveState);
}
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

function pipeMetrics() {
    return Object.fromEntries([...document.querySelectorAll("[data-pipe-metric]")]
        .map(input => [input.dataset.pipeMetric,input.checked]));
}

function radialNetworkLayout(graph) {
    const {nodes,edges} = graph;
    const links = new Map([...nodes.keys()].map(key => [key,[]]));
    const unique = new Set();
    for (const edge of edges.filter(edge => edge.run === "supply")) {
        const pair = [edge.first,edge.last].sort().join("/");
        if (unique.has(pair)) continue;
        unique.add(pair);
        links.get(edge.first)?.push(edge.last);
        links.get(edge.last)?.push(edge.first);
    }
    if (!unique.size) {
        for (const edge of edges) {
            const pair = [edge.first,edge.last].sort().join("/");
            if (unique.has(pair)) continue;
            unique.add(pair);
            links.get(edge.first)?.push(edge.last);
            links.get(edge.last)?.push(edge.first);
        }
    }
    const root = [...nodes].find(([,node]) => node.group === "sources")?.[0] ||
        [...nodes].find(([,node]) => node.group === "circuits")?.[0] || nodes.keys().next().value;
    const children = new Map([...nodes.keys()].map(key => [key,[]]));
    const parent = new Map([[root,null]]), queue = [root];
    for (let index=0;index<queue.length;index++) {
        const current = queue[index];
        const neighbors = (links.get(current) || []).slice().sort((a,b) => a.localeCompare(b));
        for (const next of neighbors) {
            if (parent.has(next)) continue;
            parent.set(next,current);
            children.get(current).push(next);
            queue.push(next);
        }
    }
    // Any disconnected equipment remains visible as a separate spoke.
    for (const key of nodes.keys()) {
        if (parent.has(key)) continue;
        parent.set(key,root);
        children.get(root).push(key);
    }
    const leafCount = key => children.get(key).length ?
        children.get(key).reduce((total,child) => total+leafCount(child),0) : 1;
    const relative = new Map([[root,{x:0,y:0}]]);
    const radius = 260;
    const placeChildren = (key,start,span,depth,parentAngle=null) => {
        const descendants = children.get(key), weights = descendants.map(leafCount);
        let cursor = start;
        descendants.forEach((child,index) => {
            const sector = span * weights[index] / weights.reduce((a,b) => a+b,0);
            const angle = descendants.length === 1 && parentAngle !== null ? parentAngle : cursor+sector/2;
            relative.set(child,{x:Math.cos(angle)*radius*(depth+1),y:Math.sin(angle)*radius*(depth+1)});
            placeChildren(child,cursor,sector,depth+1,angle);
            cursor += sector;
        });
    };
    placeChildren(root,-Math.PI,Math.PI*2,0);
    const bounds = [...relative.values()];
    const extentX = Math.max(0,...bounds.map(point => Math.abs(point.x)));
    const extentY = Math.max(0,...bounds.map(point => Math.abs(point.y)));
    const padding = 180;
    const width = Math.max(1100,extentX*2+padding*2);
    const height = Math.max(760,extentY*2+padding*2);
    const positions = new Map([...relative].map(([key,point]) =>
        [key,{x:width/2+point.x,y:height/2+point.y}]));
    return {root,positions,width,height,children};
}

function networkEdgeGeometry(first,last,run,firstGroup,lastGroup) {
    const dx=last.x-first.x, dy=last.y-first.y, length=Math.hypot(dx,dy)||1;
    const ux=dx/length, uy=dy/length, nx=-uy, ny=ux;
    const clip=(group)=> group === "junction" ? 11 :
        Math.min(96/Math.max(Math.abs(ux),.01),46/Math.max(Math.abs(uy),.01));
    const startRadius=clip(firstGroup), endRadius=clip(lastGroup), side=run === "supply" ? -9 : 9;
    const start={x:first.x+ux*startRadius+nx*side,y:first.y+uy*startRadius+ny*side};
    const end={x:last.x-ux*endRadius+nx*side,y:last.y-uy*endRadius+ny*side};
    const middle=(Math.abs(end.x-start.x)>=Math.abs(end.y-start.y)) ?
        {x:(start.x+end.x)/2,y:start.y} : {x:start.x,y:(start.y+end.y)/2};
    const other=(middle.x===start.x) ? {x:end.x,y:middle.y} : {x:middle.x,y:end.y};
    const points=`${start.x},${start.y} ${middle.x},${middle.y} ${other.x},${other.y} ${end.x},${end.y}`;
    const elbow={x:(middle.x+other.x)/2,y:(middle.y+other.y)/2};
    const label={x:elbow.x+nx*(run === "supply" ? -36 : 36),
        y:elbow.y+ny*(run === "supply" ? -36 : 36)};
    return {points,start,end,label,angle:Math.atan2(other.y-middle.y,other.x-middle.x)*180/Math.PI};
}

function nodeMetricLines(group,item,metrics) {
    const lines=[];
    if (metrics.power) {
        const power=group === "emitters" ? item.total_heat_kw :
            group === "sources" ? item.heat_kw : group === "heat_exchangers" ? item.heat_kw : null;
        if (Number.isFinite(power)) lines.push(`Heat ${format(power)} kW`);
    }
    if (metrics.flow) {
        const flow=item.flow_l_min ?? item.boiler_flow_l_min;
        if (Number.isFinite(flow)) lines.push(`Flow ${format(flow)} L/min`);
    }
    if (metrics.temperature) {
        const temperature=group === "emitters" ? item.radiator_c ?? item.supply_c :
            item.water_c ?? item.boiler_water_c ?? item.supply_c;
        if (Number.isFinite(temperature)) lines.push(`Water ${format(temperature)} °C`);
    }
    return lines;
}

function buildPiping(graph,state) {
    const {nodes,edges,inferred} = graph, layout=radialNetworkLayout(graph);
    const {positions,width,height}=layout;
    const graphic=svg("svg",{class:"live-network",width,height,viewBox:`0 0 ${width} ${height}`,
        role:"group","aria-label":"Radial water circuit diagram"});
    diagramLines=[];
    // Draw pipes first so the equipment and junctions sit clearly above them.
    for (const edge of edges) {
        const first=positions.get(edge.first),last=positions.get(edge.last);
        if (!first||!last) continue;
        const geometry=networkEdgeGeometry(first,last,edge.run,
            nodes.get(edge.first).group,nodes.get(edge.last).group);
        const path=svg("polyline",{points:geometry.points,class:`plan-pipe ${edge.run}`});
        const target=svg("polyline",{points:geometry.points,class:"pipe-target"});
        const group=inferred ? svg("g",{class:"live-pipe-item"}) :
            livePlanItem("pipes",edge.id,"live-pipe-item",path,target);
        if (inferred) group.append(path);
        const arrow=svg("path",{d:"M-5 -5 5 0-5 5",class:`network-arrow ${edge.run}`});
        const label=svg("text",{x:geometry.label.x,y:geometry.label.y,
            "text-anchor":"middle",class:"network-flow"});
        group.append(arrow,label);
        graphic.append(group);
        diagramLines.push({edge,group,arrow,label,x:(geometry.start.x+geometry.end.x)/2,
            y:(geometry.start.y+geometry.end.y)/2,angle:geometry.angle,forward:1});
    }
    for (const [key,node] of nodes) {
        const {x,y}=positions.get(key);
        if (node.group === "junction") {
            const junction=svg("g");
            for (const [run,offset] of [["supply",-9],["return",9]]) {
                if (edges.some(edge => edge.run===run&&(edge.first===key||edge.last===key))) {
                    junction.append(svg("circle",{cx:x,cy:y+offset,r:5,class:`network-junction ${run}`}));
                }
            }
            const title=svg("title"); title.textContent=human(node.name); junction.append(title);
            graphic.append(junction); continue;
        }
        const rect=svg("rect",{x:x-96,y:y-46,width:192,height:92,rx:10,class:"network-node"});
        const label=svg("text",{x:x-70,y:y-22,class:"network-title"});
        const reading=svg("text",{x:x-70,y:y-3,class:"network-reading"});
        const item=state[node.group]?.[node.name]||{};
        const config=state.configured_objects?.[node.group]?.[node.name];
        const symbol=icon(equipmentIcon(node.group,item,config));
        for (const [attribute,value] of Object.entries({x:x-86,y:y-28,width:18,height:18,class:"icon plan-device-icon"})) {
            symbol.setAttribute(attribute,value);
        }
        const name=human(node.name);
        label.textContent=name.length>22?name.slice(0,21)+"…":name;
        graphic.append(livePlanItem(node.group,node.name,"network-device",rect,symbol,label,reading));
    }
    $("live-piping-diagram").replaceChildren(graphic);
    graphic.dataset.root=layout.root;
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
        "Radial connection view inferred from circuit assignments; this circuit has no drawn pipe routes." :
        "Radial view of the drawn supply and return network. The heat source is centered; junction dots show connected branches.";
    const key = JSON.stringify([systemCircuit,[...graph.nodes],graph.edges,graph.inferred]);
    if (key !== pipingKey) { pipingKey=key; buildPiping(graph,state); }
    const metrics=pipeMetrics();
    for (const {edge,group,arrow,label,x,y,angle,forward} of diagramLines) {
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
        arrow.setAttribute("transform",`translate(${x} ${y}) rotate(${angle}) scale(${orientation} 1)`);
        arrow.style.display = active && (graph.inferred || pipe?.flow_from) ? "" : "none";
        const inlet = pipe ? pipe.inlet_c :
            (edge.run === "supply" ? emitter?.supply_c : emitter?.return_c);
        const outlet = pipe ? pipe.outlet_c : inlet;
        const values=[];
        if (metrics.flow) values.push(Number.isFinite(flow) ? `${format(flow)} L/min` : "Flow —");
        if (metrics.temperature) values.push(pipe && !(flow > .005) && Number.isFinite(pipe.water_c) ?
            `Water ${format(pipe.water_c)} °C` : Number.isFinite(inlet) && Number.isFinite(outlet) ?
            `${format(inlet)} → ${format(outlet)} °C` : Number.isFinite(pipe?.water_c) ?
                `Water ${format(pipe.water_c)} °C` : "Temperature —");
        if (metrics.power && pipe) values.push(`Room heat ${format(pipe.heat_loss_kw)} kW`);
        if (metrics.storage && pipe) values.push(`Stored ${format(pipe.stored_heat_kwh)} kWh`);
        label.replaceChildren();
        values.forEach((value,index) => {
            const line=svg("tspan",{x:label.getAttribute("x"),dy:index?13:0});
            line.textContent=value; label.append(line);
        });
        label.setAttribute("aria-label",values.join("; "));
        if (!graph.inferred) {
            const details=`${human(edge.id)} · ${values.join(" · ")}`;
            group.firstElementChild.textContent=details;
            group.setAttribute("aria-label",details);
        }
    }
    for (const node of $("live-piping-diagram").querySelectorAll(".network-device")) {
        const [group,name] = node.dataset.liveItem.split("/");
        const item = state[group]?.[name];
        const pressed = selected === node.dataset.liveItem;
        node.classList.toggle("selected",pressed);
        node.setAttribute("aria-pressed",pressed);
        const reading=node.querySelector(".network-reading");
        const lines=nodeMetricLines(group,item||{},metrics);
        reading.replaceChildren();
        lines.forEach((value,index) => {
            const line=svg("tspan",{x:reading.getAttribute("x"),dy:index?14:0});
            line.textContent=value; reading.append(line);
        });
        const title=node.firstElementChild;
        title.textContent=`${human(name)}${lines.length?` · ${lines.join(" · ")}`:""}`;
        node.setAttribute("aria-label",title.textContent);
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
                add(item(group,name),target,"equipment",value.room_heat_kw ?? value.sensible_heat_kw ?? value.heat_kw ?? value.heat_loss_kw);
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

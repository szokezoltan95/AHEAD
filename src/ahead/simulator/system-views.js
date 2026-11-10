"use strict";

let systemView = "piping", systemCircuit = "", systemCircuitsKey = "", pipingKey = "";
let diagramLines = [], diagramNodes = new Map(), diagramBasePositions = new Map();
let diagramPositions = new Map(), liveNetworkNodes = new Map(), heatRows = [], heatRowsKey = "";
const diagramLayoutKey = "ahead-piping-layout-v1";
const diagramBendLayoutKey = "ahead-piping-bends-v1";
const systemTabs = ["piping","heat","floor"];
// Keep the whole circuit visible on first load and after a page refresh.
$("live-piping-diagram").classList.add("fit-network");
$("live-fit-circuit").setAttribute("aria-pressed","true");
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
$("live-reset-layout").onclick = () => {
    const layouts=readDiagramLayouts();delete layouts[systemCircuit];writeDiagramLayouts(layouts);
    const bends=readDiagramBends();delete bends[systemCircuit];writeDiagramBends(bends);
    pipingKey="";drawNetworkViews(liveState);
};

function readDiagramLayouts() {
    try {
        const layouts=JSON.parse(localStorage.getItem(diagramLayoutKey)||"{}");
        return layouts&&typeof layouts==="object"&&!Array.isArray(layouts)?layouts:{};
    }
    catch { return {}; }
}
function writeDiagramLayouts(layouts) {
    try { localStorage.setItem(diagramLayoutKey,JSON.stringify(layouts)); } catch { /* private browsing */ }
}
function readDiagramBends() {
    try {
        const bends=JSON.parse(localStorage.getItem(diagramBendLayoutKey)||"{}");
        return bends&&typeof bends==="object"&&!Array.isArray(bends)?bends:{};
    }
    catch { return {}; }
}
function writeDiagramBends(bends) {
    try { localStorage.setItem(diagramBendLayoutKey,JSON.stringify(bends)); } catch { /* private browsing */ }
}

function circuitGraph(state,circuit) {
    const plan = state.floorplan;
    const nodes = new Map(), edges = [];
    const nodeInfo = id => {
        const node = plan?.nodes?.[id];
        return node && node.kind !== "junction" ? {group:node.kind,name:node.equipment || id} : null;
    };
    const sources = Object.entries(state.sources || {}).filter(([,item]) =>
        item.loop === circuit && !item.direct_zone);
    const sourceNode = sources.map(([name]) => Object.entries(plan?.nodes || {})
        .find(([,node]) => node.kind === "sources" && node.equipment === name)?.[0]).find(Boolean);
    const supplyRoutes=routedEquipmentPaths(state,circuit,"supply");
    const returnRoutes=routedEquipmentPaths(state,circuit,"return");
    const tapNodes={supply:new Set(),return:new Set()};
    for (const [run,routes] of [["supply",supplyRoutes],["return",returnRoutes]]) {
        const endpoints=Object.entries(routes).map(([name,route])=>[name,route.at(-1)?.[2]]);
        for (const [name,nodeId] of endpoints) {
            if (!nodeId) continue;
            if (Object.entries(routes).some(([other,route])=>other!==name&&
                route.some(([,first,last])=>first===nodeId||last===nodeId))) tapNodes[run].add(nodeId);
        }
    }
    const tapKey=id=>`shared/${id}`;
    const routeParent = (route,run) => {
        const pipes = [];
        for (let index=route.length-1;index>=0;index--) {
            const [pipe,first] = route[index];
            pipes.push(pipe);
            if (nodeInfo(first)) return {
                id:tapNodes[run].has(first)?tapKey(first):first,
                pipes:pipes.reverse(),
            };
        }
        return {id:null,pipes:pipes.reverse()};
    };
    const tapEmitters=new Map();
    for (const [name,item] of Object.entries(state.emitters || {})) {
        if (item.loop !== circuit) continue;
        const supplyRoute=supplyRoutes[name]||[];
        const returnRoute=returnRoutes[name]||[];
        const supply = routeParent(supplyRoute,"supply");
        const returning = routeParent(returnRoute,"return");
        const childId = supplyRoute.at(-1)?.[2] || returnRoute.at(-1)?.[2] ||
            Object.entries(plan?.nodes || {}).find(([,node]) => node.kind === "emitters" && node.equipment === name)?.[0] ||
            `emitter/${name}`;
        const childInfo = nodeInfo(childId) || {group:"emitters",name};
        if (!nodes.has(childId)) nodes.set(childId,childInfo);
        const isTap=tapNodes.supply.has(childId)||tapNodes.return.has(childId);
        const targetId=isTap?tapKey(childId):childId;
        if(isTap){
            nodes.set(targetId,{group:"junction",name:"Common pipe"});
            tapEmitters.set(childId,{tap:targetId,emitter:childId,name});
        }
        const branches = [
            {run:"supply",route:supplyRoute,parent:supply},
            {run:"return",route:returnRoute,parent:returning},
        ];
        if (!supplyRoute.length && !returnRoute.length) {
            branches[0].parent={id:sourceNode||`circuit/${circuit}`,pipes:[]};
        }
        for (const branch of branches) {
            if (!branch.route.length && (branch.run!=="supply" || supplyRoute.length || returnRoute.length)) continue;
            let parentId=branch.parent.id;
            // Replace a circuit manifold with the boiler/source node at the hub.
            if (nodeInfo(parentId)?.group === "circuits" && sourceNode) parentId=sourceNode;
            if (!parentId) parentId=sourceNode||`circuit/${circuit}`;
            if (!nodes.has(parentId)) {
                const info=nodeInfo(parentId)||(parentId.startsWith("circuit/")?
                    {group:"circuits",name:circuit}:null);
                if (info) nodes.set(parentId,info);
            }
            if (parentId===targetId) continue;
            edges.push({
                id:`${branch.run}/${parentId}/${targetId}`,first:parentId,last:targetId,
                run:branch.run,emitter:name,pipes:branch.parent.pipes,sharedTrunk:isTap,
            });
        }
    }
    for(const {tap,emitter,name} of tapEmitters.values()) for(const run of ["supply","return"]){
        if(!edges.some(edge=>edge.run===run&&edge.first===tap&&edge.last===emitter))
            edges.push({id:`${run}/${tap}/${emitter}`,first:tap,last:emitter,run,emitter:name,
                pipes:[],virtual:true});
    }
    return {nodes,edges,inferred:!Object.keys(supplyRoutes).length};
}

function pipeMetrics() {
    return Object.fromEntries([...document.querySelectorAll("[data-pipe-metric]")]
        .map(input => [input.dataset.pipeMetric,input.checked]));
}

function routedEquipmentPaths(state,circuit,run) {
    const plan=state.floorplan,nodes=plan?.nodes||{},adjacency=new Map();
    for (const [id,pipe] of Object.entries(plan?.pipes||{})) {
        if (pipe.circuit!==circuit||pipe.run!==run) continue;
        for (const [first,last] of [[pipe.first,pipe.last],[pipe.last,pipe.first]]) {
            if (!adjacency.has(first)) adjacency.set(first,[]);
            adjacency.get(first).push([last,id]);
        }
    }
    const roots=Object.entries(nodes).filter(([,node]) =>
        (node.kind==="circuits"&&node.equipment===circuit) ||
        (node.kind==="sources"&&state.sources?.[node.equipment]?.loop===circuit&&
         !state.sources?.[node.equipment]?.direct_zone))
        .map(([id]) => id).sort();
    const previous=new Map(roots.map(id=>[id,null])),queue=roots.slice();
    for (let index=0;index<queue.length;index++) {
        const first=queue[index];
        for (const [last,pipe] of adjacency.get(first)||[]) {
            if (previous.has(last)) continue;
            previous.set(last,[first,pipe]); queue.push(last);
        }
    }
    const paths={};
    for (const [nodeId,node] of Object.entries(nodes)) {
        if (node.kind!=="emitters"||state.emitters?.[node.equipment]?.loop!==circuit||!previous.has(nodeId)) continue;
        const reverse=[]; let point=nodeId;
        while (previous.get(point)) {
            const [parent,pipe]=previous.get(point);
            reverse.push([pipe,parent,point]); point=parent;
        }
        if (reverse.length) paths[node.equipment]=reverse.reverse();
    }
    return paths;
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
    const radius = 220;
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
    const padding = 145;
    const width = Math.max(960,extentX*2+padding*2);
    const height = Math.max(640,extentY*2+padding*2);
    const positions = new Map([...relative].map(([key,point]) =>
        [key,{x:width/2+point.x,y:height/2+point.y}]));
    return {root,positions,width,height,children};
}

function networkEdgeGeometry(first,last,run,firstGroup,lastGroup,bend="horizontal") {
    const dx=last.x-first.x, dy=last.y-first.y, length=Math.hypot(dx,dy)||1;
    const ux=dx/length, uy=dy/length, nx=-uy, ny=ux;
    const clip=(group)=> group === "junction" ? 11 :
        Math.min(96/Math.max(Math.abs(ux),.01),46/Math.max(Math.abs(uy),.01));
    const startRadius=clip(firstGroup), endRadius=clip(lastGroup), side=run === "supply" ? -9 : 9;
    const start={x:first.x+ux*startRadius+nx*side,y:first.y+uy*startRadius+ny*side};
    const end={x:last.x-ux*endRadius+nx*side,y:last.y-uy*endRadius+ny*side};
    const turn=bend==="vertical"?{x:start.x,y:end.y}:{x:end.x,y:start.y};
    const points=`${start.x},${start.y} ${turn.x},${turn.y} ${end.x},${end.y}`;
    const label=bend==="vertical"?
        {x:start.x+(run === "supply" ? -30 : 30),y:(start.y+turn.y)/2}:
        {x:(start.x+turn.x)/2,y:start.y+(run === "supply" ? -26 : 26)};
    const arrow={x:(turn.x+end.x)/2,y:(turn.y+end.y)/2};
    return {points,start,end,turn,label,arrow,
        angle:Math.atan2(end.y-turn.y,end.x-turn.x)*180/Math.PI};
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
    const {nodes,edges} = graph, layout=radialNetworkLayout(graph);
    liveNetworkNodes=nodes;
    const {width,height}=layout;
    diagramBasePositions=new Map([...layout.positions].map(([key,value])=>[key,{...value}]));
    const saved=readDiagramLayouts()[systemCircuit]||{};
    const savedBends=readDiagramBends()[systemCircuit]||{};
    diagramPositions=new Map([...diagramBasePositions].map(([key,value])=>[
        key,Number.isFinite(saved[key]?.x)&&Number.isFinite(saved[key]?.y)?
            {x:Math.max(100,Math.min(width-100,saved[key].x)),y:Math.max(100,Math.min(height-100,saved[key].y))}:{...value},
    ]));
    const positions=diagramPositions;
    const graphic=svg("svg",{class:"live-network",width,height,viewBox:`0 0 ${width} ${height}`,
        role:"group","aria-label":"Radial water circuit diagram"});
    diagramLines=[];diagramNodes=new Map();
    // Draw one summarized connection per actual supply or return topology;
    // intermediate junctions and pipe segments are collapsed.
    for (const edge of edges) {
        const first=positions.get(edge.first),last=positions.get(edge.last);
        if (!first||!last) continue;
        const bend=savedBends[edge.id]==="vertical"?"vertical":"horizontal";
        const geometry=networkEdgeGeometry(first,last,edge.run,
            nodes.get(edge.first).group,nodes.get(edge.last).group,bend);
        const path=svg("polyline",{points:geometry.points,class:`plan-pipe ${edge.run}${edge.virtual?" diagram-link":""}`});
        const target=svg("polyline",{points:geometry.points,class:"pipe-target"});
        const arrow=svg("path",{d:"M-5 -5 5 0-5 5",class:`network-arrow ${edge.run}`});
        const label=svg("text",{x:geometry.label.x,y:geometry.label.y,
            "text-anchor":"middle",class:"network-flow"});
        const group=livePlanItem("emitters",edge.emitter,"live-pipe-item",
            path,target,arrow,label);
        graphic.append(group);
        const bendHandle=edge.virtual?null:svg("circle",{
            cx:geometry.turn.x,cy:geometry.turn.y,r:8,class:"network-bend-handle",
            "data-layout-bend":edge.id,tabindex:"0",role:"button",
            "aria-label":`Pipe elbow; drag to switch between horizontal-first and vertical-first routing`,
        });
        if(bendHandle){const title=svg("title");title.textContent="Drag elbow to change pipe starting direction";bendHandle.append(title);}
        diagramLines.push({edge,group,label,path,target,bend,bendHandle,arrows:[
            {node:arrow,x:geometry.arrow.x,y:geometry.arrow.y,
                angle:geometry.angle,orientation:edge.run==="supply"?1:-1},
        ]});
    }
    for (const [key,node] of nodes) {
        // Draw each node at its base position; the saved offset is applied to
        // its group below. Using the saved position here as well doubles the
        // offset every time a saved layout is restored after a refresh.
        const {x,y}=diagramBasePositions.get(key);
        if (node.group === "junction") {
            const junction=svg("g",{class:"network-junction-node layout-draggable",
                "data-layout-node":key,tabindex:"0",role:"button","aria-label":`${node.name}; drag to move`});
            for (const [run,offset] of [["supply",-9],["return",9]]) {
                if (edges.some(edge => edge.run===run&&(edge.first===key||edge.last===key))) {
                    junction.append(svg("circle",{cx:x,cy:y+offset,r:7,class:`network-junction ${run}`}));
                }
            }
            if(node.name==="Common pipe"){
                const title=svg("text",{x:x+13,y:y-14,class:"network-junction-title"});
                title.textContent="Common pipe";junction.append(title);
            }
            const title=svg("title"); title.textContent=human(node.name); junction.append(title);
            graphic.append(junction);diagramNodes.set(key,junction);continue;
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
        const device=livePlanItem(node.group,node.name,"network-device",rect,symbol,label,reading);
        device.classList.add("layout-draggable");device.dataset.layoutNode=key;
        device.setAttribute("tabindex","0");device.setAttribute("aria-label",`${name}; drag to move`);
        graphic.append(device);diagramNodes.set(key,device);
    }
    for(const item of diagramLines)if(item.bendHandle)graphic.append(item.bendHandle);
    for(const key of diagramNodes.keys())applyDiagramNodePosition(key);
    bindDiagramDragging(graphic);
    $("live-piping-diagram").replaceChildren(graphic);
    graphic.dataset.root=layout.root;
}

function applyDiagramNodePosition(key) {
    const element=diagramNodes.get(key),base=diagramBasePositions.get(key),position=diagramPositions.get(key);
    if(!element||!base||!position)return;
    element.setAttribute("transform",`translate(${position.x-base.x} ${position.y-base.y})`);
}

function repositionDiagramEdge(item) {
    const {edge,path,target,label,arrows}=item;
    const first=diagramPositions.get(edge.first),last=diagramPositions.get(edge.last);
    if(!first||!last)return;
    const geometry=networkEdgeGeometry(first,last,edge.run,
        liveNetworkNodes.get(edge.first)?.group,liveNetworkNodes.get(edge.last)?.group,item.bend);
    path.setAttribute("points",geometry.points);target.setAttribute("points",geometry.points);
    label.setAttribute("x",geometry.label.x);label.setAttribute("y",geometry.label.y);
    if(item.bendHandle){item.bendHandle.setAttribute("cx",geometry.turn.x);item.bendHandle.setAttribute("cy",geometry.turn.y);}
    arrows.forEach(arrow=>{
        const {node,orientation}=arrow;
        arrow.x=geometry.arrow.x;arrow.y=geometry.arrow.y;arrow.angle=geometry.angle;
        node.setAttribute("transform",`translate(${geometry.arrow.x} ${geometry.arrow.y}) rotate(${geometry.angle}) scale(${orientation} 1)`);
    });
}

function bindDiagramDragging(graphic) {
    let drag=null;
    const pointAt=event=>{
        const point=graphic.createSVGPoint();point.x=event.clientX;point.y=event.clientY;
        return point.matrixTransform(graphic.getScreenCTM().inverse());
    };
    graphic.addEventListener("pointerdown",event=>{
        const handle=event.target.closest("[data-layout-bend]");
        if(handle&&event.button===0){
            const item=diagramLines.find(line=>line.edge.id===handle.dataset.layoutBend);
            if(!item)return;
            event.preventDefault();event.stopPropagation();
            drag={type:"bend",item,pointerId:event.pointerId,start:pointAt(event),moved:false};
            graphic.setPointerCapture(event.pointerId);handle.classList.add("dragging");
            return;
        }
        const node=event.target.closest("[data-layout-node]");
        if(!node||event.button!==0)return;
        const key=node.dataset.layoutNode,position=diagramPositions.get(key);
        if(!position)return;
        event.preventDefault();event.stopPropagation();
        const point=pointAt(event);
        drag={type:"node",key,node,pointerId:event.pointerId,start:point,origin:{...position},moved:false};
        graphic.setPointerCapture(event.pointerId);node.classList.add("dragging");
    });
    graphic.addEventListener("pointermove",event=>{
        if(!drag||event.pointerId!==drag.pointerId)return;
        const point=pointAt(event);
        if(drag.type==="bend"){
            const item=drag.item,{edge}=item;
            const first=diagramPositions.get(edge.first),last=diagramPositions.get(edge.last);
            const groups=[liveNetworkNodes.get(edge.first)?.group,liveNetworkNodes.get(edge.last)?.group];
            const options=["horizontal","vertical"].map(axis=>({axis,turn:networkEdgeGeometry(
                first,last,edge.run,groups[0],groups[1],axis).turn}));
            const distance=option=>Math.hypot(point.x-option.turn.x,point.y-option.turn.y);
            const current=options.find(option=>option.axis===item.bend);
            const other=options.find(option=>option.axis!==item.bend);
            drag.moved ||= Math.hypot(point.x-drag.start.x,point.y-drag.start.y)>2;
            if(distance(other)+8<distance(current))item.bend=other.axis;
            repositionDiagramEdge(item);
            return;
        }
        const position={
            x:Math.max(100,Math.min(Number(graphic.getAttribute("width"))-100,drag.origin.x+point.x-drag.start.x)),
            y:Math.max(100,Math.min(Number(graphic.getAttribute("height"))-100,drag.origin.y+point.y-drag.start.y)),
        };
        drag.moved ||= Math.hypot(point.x-drag.start.x,point.y-drag.start.y)>2;
        diagramPositions.set(drag.key,position);applyDiagramNodePosition(drag.key);
        for(const item of diagramLines)repositionDiagramEdge(item);
    });
    const finish=event=>{
        if(!drag||event.pointerId!==drag.pointerId)return;
        if(drag.type==="bend"){
            const item=drag.item;item.bendHandle.classList.remove("dragging");
            const moved=drag.moved;drag=null;
            if(moved){
                const bends=readDiagramBends();
                bends[systemCircuit]=Object.fromEntries(diagramLines.filter(line=>!line.edge.virtual)
                    .map(line=>[line.edge.id,line.bend]));
                writeDiagramBends(bends);
            }
            return;
        }
        const node=drag.node,moved=drag.moved;node.classList.remove("dragging");drag=null;
        if(moved){
            const layouts=readDiagramLayouts();
            layouts[systemCircuit]=Object.fromEntries([...diagramPositions].map(([key,value])=>[key,{...value}]));
            writeDiagramLayouts(layouts);
        }
    };
    graphic.addEventListener("pointerup",finish);graphic.addEventListener("pointercancel",finish);
}

function drawPiping(state) {
    const circuits = Object.keys(state.circuits || {});
    const available = circuits.length > 0;
    $("live-circuit-control").hidden = !available;
    $("live-fit-circuit").hidden = !available;
    $("live-reset-layout").hidden = !available;
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
        "Simplified branches inferred from circuit assignments. Drag nodes to arrange them and pipe elbows to choose horizontal-first or vertical-first routing. Changes are saved in this browser." :
        "Equipment-level topology with shared pipe taps shown as junctions. Drag nodes to arrange them and pipe elbows to choose horizontal-first or vertical-first routing. Changes are saved in this browser.";
    const key = JSON.stringify([systemCircuit,[...graph.nodes],graph.edges,graph.inferred]);
    if (key !== pipingKey) { pipingKey=key; buildPiping(graph,state); }
    const metrics=pipeMetrics();
    for (const {edge,group,arrows,label} of diagramLines) {
        if(edge.virtual){
            label.replaceChildren();label.setAttribute("aria-label","Shared-pipe radiator tap");
            group.dataset.state="flowing";group.setAttribute("aria-label","Radiator tap at shared pipe");
            arrows.forEach(({node})=>node.style.display="none");
            continue;
        }
        const emitter = state.emitters?.[edge.emitter];
        const pipeValues = ids => ids.map(id => state.pipes?.[id]).filter(Boolean);
        const segments = pipeValues(edge.pipes);
        const mean = (items,key) => {
            const values=items.map(item=>item[key]).filter(Number.isFinite);
            return values.length ? values.reduce((sum,value)=>sum+value,0)/values.length : null;
        };
        const flow = mean(segments,"flow_l_min") ?? emitter?.flow_l_min;
        const active = Number.isFinite(flow) && flow > .005;
        group.dataset.state = active ? "flowing" : "idle";
        const pressed = selected === "emitters/"+edge.emitter;
        group.classList.toggle("selected",pressed);
        group.setAttribute("aria-pressed",pressed);
        const water = mean(segments,"water_c") ?? (edge.run==="supply"?emitter?.supply_c:emitter?.return_c);
        const values=[];
        if (metrics.flow) values.push(Number.isFinite(flow) ? `${format(flow)} L/min` : "Flow —");
        if (metrics.temperature) values.push(Number.isFinite(water) ?
            `${edge.run} ${format(water)} °C` : `${edge.run} temperature —`);
        if (!edge.sharedTrunk&&edge.run==="supply"&&metrics.power&&Number.isFinite(emitter?.total_heat_kw)) values.push(`Heat ${format(emitter.total_heat_kw)} kW`);
        if (metrics.storage && segments.length) {
            const stored=segments.map(item=>item.stored_heat_kwh).filter(Number.isFinite);
            if (stored.length) values.push(`Avg stored ${format(stored.reduce((a,b)=>a+b,0)/stored.length)} kWh/segment`);
        }
        label.replaceChildren();
        values.forEach((value,index) => {
            const line=svg("tspan",{x:label.getAttribute("x"),dy:index?13:0});
            line.textContent=value; label.append(line);
        });
        label.setAttribute("aria-label",values.join("; "));
        const details=`${edge.sharedTrunk?"Shared pipe · ":`${human(edge.emitter)} · `}${values.join(" · ")}`;
        if(group.firstElementChild?.tagName!="polyline")group.firstElementChild.textContent=details;
        group.setAttribute("aria-label",details);
        arrows.forEach(({node,x,y,angle,orientation})=>{
            node.setAttribute("transform",`translate(${x} ${y}) rotate(${angle}) scale(${orientation} 1)`);
            node.style.display=active?"":"none";
        });
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

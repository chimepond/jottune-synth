// Copyright (c) 2026 Nenad Micic
// SPDX-License-Identifier: Apache-2.0
// Projection only: native sampling owns all playback coordinates and yaw.
const SVG = 'http://www.w3.org/2000/svg';

export function projectSpatial(point, extent, view = 'top') {
  const elevation = view === 'top' ? Math.PI / 2 : Math.PI / 6;
  const azimuth = view === 'top' ? 0 : Math.PI / 6;
  const scale = 120 / Math.max(1,extent);
  const horizontal = point.x * Math.cos(azimuth) - point.y * Math.sin(azimuth);
  const depth = point.x * Math.sin(azimuth) + point.y * Math.cos(azimuth);
  return [200 + horizontal * scale,155 - (depth * Math.sin(elevation) + point.z * Math.cos(elevation)) * scale];
}

export function createSpatialView(document, {colors,onSelectVoice} = {}) {
  const make = (tag, id, text) => {
    const node = document.createElement(tag);if (id) node.id = id;if (text) node.textContent = text;return node;
  };
  const svg = (tag,attributes = {}) => {
    const node = document.createElementNS(SVG,tag);
    for (const [key,value] of Object.entries(attributes)) node.setAttribute(key,String(value));
    return node;
  };
  const element = make('section', 'instrument-spatial-panel');element.className = 'instrument-spatial';
  element.setAttribute('aria-label','Spatial scene');
  const heading = make('h3','instrument-space-heading','Spatial targets');
  const controls = make('div',null);controls.className = 'instrument-space-controls';
  controls.setAttribute('role','group');controls.setAttribute('aria-label','Spatial projection');
  const buttons = new Map();
  for (const [mode,label] of [['top','Top'],['3d','3D']]) {
    const button = make('button','instrument-space-'+mode,label);button.type = 'button';
    controls.append(button);buttons.set(mode,button);
  }
  const plot = svg('svg',{id:'instrument-space',viewBox:'0 0 400 320',role:'img'}), description = svg('desc');
  const floor = svg('g'),paths = svg('g',{id:'instrument-space-paths'}),drops = svg('g'),dots = svg('g');
  const listener = svg('g'), listenerArrow = svg('path',{stroke:'#f2f7fa','stroke-width':2,fill:'none'});
  listener.append(svg('circle',{cx:200,cy:155,r:7,fill:'#101a20',stroke:'#f2f7fa','stroke-width':2}),listenerArrow);
  // One fixed edge label keeps even coincident voices readable without moving
  // native dots or adding a dense web of leader lines.
  const selection = svg('g',{id:'instrument-space-selection',display:'none','pointer-events':'none','aria-hidden':'true'});
  const leader = svg('line',{id:'instrument-space-leader',x1:106,y1:22,'stroke-width':1.25,opacity:.7});
  const selectedRing = svg('circle',{r:11,fill:'none','stroke-width':1.5});
  const labelBox = svg('rect',{x:8,y:7,width:98,height:29,rx:5,fill:'#101a20','stroke-width':1});
  const selectedLabel = svg('text',{id:'instrument-space-label',x:18,y:27,'font-size':16});
  selection.append(leader,selectedRing,labelBox,selectedLabel);
  plot.append(description,floor,paths,drops,dots,listener,selection);
  const summary = make('p','instrument-space-summary');summary.className = 'instrument-caption';
  const note = make('p','instrument-space-note');note.className = 'instrument-caption';
  element.append(heading,controls,plot,summary,note);
  let model = null, mode = 'top', extent = 1000, lastSnapshot = null, selected = 0, geometryKey = '', nodes = [];
  const point = value => projectSpatial(value,extent,mode);
  const pathData = points => points.map((value,index) => `${index ? 'L' : 'M'}${point(value).join(' ')}`).join(' ');
  const text = (position,label) => {
    const [x,y] = position,node = svg('text',{x,y,fill:'#c6d8e1','font-size':16});node.textContent = label;return node;
  };
  function geometry(native) {
    const key = `${native}:${mode}:${selected}`;if (key === geometryKey) return;geometryKey = key;
    floor.replaceChildren();paths.replaceChildren();
    const ring = radius => Array.from({length:73},(_,index) => {
      const angle = index * Math.PI / 36;return {x:Math.cos(angle)*radius,y:Math.sin(angle)*radius,z:0};
    });
    const step = 1000 * 10 ** Math.max(0,Math.floor(Math.log10(extent / 1000)));
    for (let radius = step;radius <= extent;radius += step)
      floor.append(svg('path',{d:pathData(ring(radius)),stroke:'#304650','stroke-width':1,fill:'none'}));
    if (native && model.spatial.near > 0)
      floor.append(svg('path',{d:pathData(ring(model.spatial.near)),stroke:'#eabf6c','stroke-dasharray':'4 4',fill:'none'}));
    const origin = {x:0,y:0,z:0}, size = extent*.86;
    const axes = [[{x:size,y:0,z:0},'x / right'],[{x:0,y:size,z:0},'y / front']];
    if (mode === '3d') axes.push([{x:0,y:0,z:size},'z / up']);
    for (const [end,label] of axes) {
      floor.append(svg('path',{d:pathData([origin,end]),stroke:'#708998','stroke-width':1}));
      const [x,y] = point(end);floor.append(text([x+5,y-6],label));
    }
    for (const voice of model.voices) {
      const points = native ? model.spatialPaths[voice.voice] : voice.commands;
      if (!native && voice.voice !== selected) continue;
      let solid = '',jump = '';
      for (let index=1;index<points.length;index++) {
        const segment = pathData([points[index-1],points[index]]);
        if (native && points[index].pen) solid += segment;else jump += segment;
      }
      const attributes = {fill:'none',stroke:colors[voice.voice % colors.length],
        'stroke-width':voice.voice === selected ? 2 : 1,opacity:voice.voice === selected ? .95 : .4};
      const line = svg('path',{...attributes,d:solid}), dotted = svg('path',{...attributes,d:jump,'stroke-dasharray':'2 4'});
      line.dataset.pathVoice = dotted.dataset.pathVoice = String(voice.voice);
      line.dataset.pathKind = 'motion';dotted.dataset.pathKind = native ? 'jump' : 'command-order';paths.append(line,dotted);
    }
  }
  function update(snapshot, voice = selected) {
    if (!model) return;lastSnapshot = snapshot;selected = voice;
    const native = !!snapshot.spatial;geometry(native);
    let selectedPosition = null;
    for (const state of snapshot.voices) {
      const node = nodes[state.voice], position = state.position || state.command;
      if (!position) {
        node.dot.setAttribute('display','none');node.drop.setAttribute('d','');
        for (const key of ['targetVoice','positionKind','timeNs','x','y','z']) delete node.dot.dataset[key];continue;
      }
      const [x,y] = point(position), active = state.gate.gate;
      node.dot.removeAttribute('display');
      node.dot.setAttribute('cx',x);node.dot.setAttribute('cy',y);
      node.dot.setAttribute('r',state.voice === selected ? 8 : active ? 7 : 5);
      node.dot.setAttribute('opacity',active || state.voice === selected ? 1 : .7);
      node.dot.dataset.targetVoice = String(state.voice);node.dot.dataset.positionKind = native ? 'native' : 'target';
      for (const coordinate of ['x','y','z']) node.dot.dataset[coordinate] = String(position[coordinate]);
      if (state.command) node.dot.dataset.timeNs = String(state.command.time_ns);else delete node.dot.dataset.timeNs;
      if (state.voice === selected) selectedPosition = [x,y];
      node.drop.setAttribute('d',position.z && mode === '3d' ? pathData([position,{...position,z:0}]) : '');
    }
    if (selectedPosition) {
      const [x,y] = selectedPosition, color = colors[selected % colors.length];
      selection.removeAttribute('display');selectedLabel.textContent = `Voice ${selected}`;
      selectedLabel.setAttribute('fill',color);
      for (const node of [leader,selectedRing,labelBox]) node.setAttribute('stroke',color);
      leader.setAttribute('x2',x);leader.setAttribute('y2',y);
      selectedRing.setAttribute('cx',x);selectedRing.setAttribute('cy',y);
    } else {selection.setAttribute('display','none');selectedLabel.textContent = '';}
    const yaw = native ? snapshot.spatial.yaw : null;
    if (yaw !== null) {
      const angle = yaw / 1000 * Math.PI / 180,length = extent*.22;
      listenerArrow.setAttribute('d',pathData([{x:0,y:0,z:0},{x:Math.sin(angle)*length,y:Math.cos(angle)*length,z:0}]));
    } else listenerArrow.setAttribute('d','');
    plot.dataset.positionKind = native ? 'native' : 'target';plot.dataset.projection = mode;
    plot.dataset.frame = String(snapshot.spatial?.frame ?? snapshot.frame);
    heading.textContent = native ? 'Space · native positions' : 'Spatial command targets';
    summary.textContent = `${(extent*2/1000).toFixed(1)} m across · listener at origin${yaw === null ? ' · orientation unavailable' : ` · yaw ${(yaw/1000).toFixed(1)}°`}`;
    note.textContent = (native ? `Solid paths show native motion; dotted links are jumps. The gold ring marks the ${(model.spatial.near/1000).toFixed(2)} m near distance. Brightness follows note gates, not release tails.`
      : model.spatialWarning ? `${model.spatialWarning} Showing command targets only; dotted links show command order.`
        : 'Command targets only, not current positions. Dotted links show the selected voice’s command order.')
      + ' Only the selected voice is labeled. Select a voice below to identify its dot and highlight its path.';
    const label = `${mode === 'top' ? 'Top' : '3D'} view, x right, y front, z up. ${native ? 'Native spatial playback coordinates and listener yaw.' : 'Command targets, not interpolated positions; listener orientation unavailable.'}`;
    const identification = selectedPosition ? `Voice ${selected} is identified by the edge label and leader line.` : `Voice ${selected} has no position to label.`;
    plot.setAttribute('aria-label',label+' '+identification);description.textContent = label+' '+identification+' '+summary.textContent;
  }
  for (const [value,button] of buttons) button.addEventListener('click',() => {
    mode = value;for (const [key,item] of buttons) item.setAttribute('aria-pressed',String(key === mode));
    if (lastSnapshot) update(lastSnapshot,selected);
  });
  function clear() {
    model = null;lastSnapshot = null;selected = 0;geometryKey = '';nodes = [];
    for (const group of [floor,paths,drops,dots]) group.replaceChildren();
    selection.setAttribute('display','none');selectedLabel.textContent = '';
    listenerArrow.setAttribute('d','');description.textContent = 'No spatial data loaded.';plot.setAttribute('aria-label','No spatial data loaded.');
    heading.textContent = 'Spatial targets';summary.textContent = '';note.textContent = 'Load a song to inspect spatial targets and paths.';
    for (const key of ['positionKind','projection','frame']) delete plot.dataset[key];
    for (const [value,button] of buttons) {button.disabled = true;button.setAttribute('aria-pressed',String(value === mode));}
  }
  function setModel(value) {
    clear();model = value;extent = model.spatialExtent || model.extent*1.2;
    for (const button of buttons.values()) button.disabled = false;
    for (const voice of model.voices) {
      const color = colors[voice.voice % colors.length];
      const dot = svg('circle',{fill:color,stroke:'#101a20','stroke-width':1,display:'none'});
      const drop = svg('path',{stroke:color,opacity:.6,'stroke-dasharray':'3 3',fill:'none'});
      dot.addEventListener('click',() => onSelectVoice?.(voice.voice));
      dots.append(dot);drops.append(drop);nodes.push({dot,drop});
    }
  }
  clear();return {element,setModel,update,clear};
}

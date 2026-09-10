// Reports where every run of text actually landed and how it is set. Read back
// out of the DOM by `audit`, which compares it against design.json — a band
// score says a region is wrong, this says which string and by how much.
// Wait for the webfont before measuring anything. This script used to run
// synchronously at the end of the body, which is before the face has loaded,
// so every rect described the fallback: a heading that is three lines in
// Raleway measured four, and boxes were widened to fit a font the page never
// actually uses.
function cls(el){
  return (el.getAttribute('class')||el.tagName||'').toString().slice(0,44);
}
function fwMeasure(){
  var out=[], secs=[], seen=new Set(),
      root=document.querySelector('.mc-page')||document.body;
  root.querySelectorAll('section,[data-section]').forEach(function(el){
    var r=el.getBoundingClientRect();
    if(r.height>40) secs.push({
      name:(el.getAttribute('data-section')||(el.getAttribute('class')||'').split(' ')[0]||'section'),
      y:Math.round(r.top+window.scrollY), h:Math.round(r.height)});
  });
  function sectionOf(el){
    var p=el;
    while(p&&p!==root){
      if(p.tagName==='SECTION'||p.hasAttribute('data-section'))
        return (p.getAttribute('data-section')||(p.getAttribute('class')||'').split(' ')[0]||'section');
      p=p.parentElement;
    }
    return null;
  }
  // A heading split by an inline <span> reports the span's own rect, so
  // "Core <span>Capabilities</span>" measures at the second line while the
  // design node is the whole heading and measures at the first. Carry the
  // enclosing block too, and let `audit` use it when the page run is only a
  // fragment of the design string.
  // An accordion answer inside max-height:0;overflow:hidden still reports a
  // rect, stacked at the collapsed position. Measuring it said the FAQ was
  // 550px short and produced six identical -125px gaps that describe the
  // closed state, not the design.
  function clipped(el){
    var p=el;
    while(p&&p!==root){
      var c=getComputedStyle(p);
      if(c.overflow!=='visible'&&p.clientHeight===0) return true;
      p=p.parentElement;
    }
    return false;
  }
  function blockOf(el){
    var p=el;
    while(p&&p!==root){
      var d=getComputedStyle(p).display;
      if(d==='block'||d==='flex'||d==='grid'||d==='list-item') return p;
      p=p.parentElement;
    }
    return el;
  }
  var w=document.createTreeWalker(root, NodeFilter.SHOW_TEXT), n;
  while((n=w.nextNode())){
    var t=(n.textContent||'').trim();
    if(t.length<3) continue;
    var el=n.parentElement; if(!el||seen.has(el)) continue; seen.add(el);
    var inner=false, p=el.parentElement;
    while(p&&p!==root){ if(seen.has(p)){inner=true;break;} p=p.parentElement; }
    var r=el.getBoundingClientRect(); if(!r.width||!r.height) continue;
    var c=getComputedStyle(el), b=blockOf(el).getBoundingClientRect();
    out.push({text:t.slice(0,90),
              x:Math.round(r.left+window.scrollX), y:Math.round(r.top+window.scrollY),
              w:Math.round(r.width), h:Math.round(r.height),
              size:parseFloat(c.fontSize), lh:c.lineHeight, ls:c.letterSpacing,
              family:(c.fontFamily||'').split(',')[0].replace(/["']/g,'').trim(),
              weight:c.fontWeight, color:c.color,
              by:Math.round(b.top+window.scrollY), bh:Math.round(b.height),
              clipped:clipped(el),
              cls:(el.getAttribute('class')||'').slice(0,60), inner:inner,
              // A centred block's left edge says nothing: widen the box and
              // the text does not move, narrow it and it does. Comparing
              // left-to-left reported a heading 267px out that was sitting
              // exactly where the frame puts it.
              align: c.textAlign,
              // Where the ink starts, not where the box does. A list item
              // with 32px of padding for its bullet reports its border box
              // at the card's inset while the frame's text node sits 32px
              // in — 13 items all reported 32px out, none of them wrong.
              padl: (parseFloat(c.borderLeftWidth)||0) + (parseFloat(c.paddingLeft)||0),
              padr: (parseFloat(c.borderRightWidth)||0) + (parseFloat(c.paddingRight)||0),
              padt: (parseFloat(c.borderTopWidth)||0) + (parseFloat(c.paddingTop)||0),
              padb: (parseFloat(c.borderBottomWidth)||0) + (parseFloat(c.paddingBottom)||0),
              br: el.getElementsByTagName ? el.getElementsByTagName('br').length : 0,
              // Inside a rotating row this element's x depends on which
              // slide is up, not on the design. It cannot vote on where
              // the page sits, the same way it cannot set the gutter.
              loop: !!(el.closest && el.closest('.mc-loop')),
              sec:sectionOf(el)});
  }
  // Boxes, not just text. design.json carries 305 frames with w/h/pad/gap/
  // radius/stroke/rotation and until now nothing on the page was ever compared
  // against any of them — which is why a chevron got built from pixels
  // measured off the render instead of from the file.
  var boxes=[];
  function num(v){ var f=parseFloat(v); return isNaN(f)?0:Math.round(f*10)/10; }
  root.querySelectorAll('*').forEach(function(el){
    var r=el.getBoundingClientRect();
    if(r.width<6||r.height<6) return;
    if(clipped(el)) return;
    var c=getComputedStyle(el);
    var bw=num(c.borderTopWidth)||num(c.borderLeftWidth);
    var bg=c.backgroundColor, hasBg=bg&&bg!=='rgba(0, 0, 0, 0)'&&bg!=='transparent';
    var rad=num(c.borderTopLeftRadius);
    var tf=c.transform&&c.transform!=='none';
    var svg=el.tagName==='svg'||el.tagName==='SVG';
    // A gradient is background-image, not background-color, so the product
    // panel — a 600x720 gradient — was never measured at all. Media has a
    // frame in the file whether or not it is painted.
    var grad=c.backgroundImage&&c.backgroundImage!=='none';
    var media=el.tagName==='IMG'||el.tagName==='VIDEO';
    // Only things with a drawn edge, a fill, a corner, a transform, an SVG or
    // a picture — a bare layout <div> has nothing in the file to compare with.
    if(!(bw||hasBg||grad||rad||tf||svg||media)) return;
    var stroke=null, sel=el.querySelector&&el.querySelector('[stroke-width]');
    if(svg&&sel) stroke={w:num(sel.getAttribute('stroke-width')), color:c.color};
    else if(bw) stroke={w:bw, color:c.borderTopColor};
    var rot=null;
    if(tf){ var m=/matrix\(([-\d.]+),\s*([-\d.]+)/.exec(c.transform);
            if(m) rot=Math.round(Math.atan2(parseFloat(m[2]),parseFloat(m[1]))*180/Math.PI*10)/10; }
    boxes.push({tag:el.tagName.toLowerCase(),
      cls:(el.getAttribute('class')||'').toString().slice(0,44),
      x:Math.round(r.left+window.scrollX), y:Math.round(r.top+window.scrollY),
      w:Math.round(r.width), h:Math.round(r.height),
      pad:[num(c.paddingTop),num(c.paddingRight),num(c.paddingBottom),num(c.paddingLeft)],
      gap:num(c.gap)||num(c.columnGap)||0,
      // What the children are actually spaced by, however it was written.
      // Figma says itemSpacing 20; the page can reach the same 20 with a
      // margin, and reporting "gap 0 vs 20" on two identical layouts is a
      // complaint about the mechanism, not the result.
      childGap:(function(){
        var k=[].slice.call(el.children).map(function(x){return x.getBoundingClientRect();})
                .filter(function(r){return r.width&&r.height;});
        if(k.length<2) return null;
        var vert=k[1].top>=k[0].bottom-1;
        return Math.round(vert? k[1].top-k[0].bottom : k[1].left-k[0].right);
      })(),
      radius:rad, radiusRaw:c.borderTopLeftRadius, bg:hasBg?bg:null, stroke:stroke, rot:rot,
      shadow:(c.boxShadow&&c.boxShadow!=='none')?c.boxShadow:null,
      hasStrokedChild: !!(el.querySelector && el.querySelector('svg [stroke-width]')),
      sec:sectionOf(el)});
  });

  var s=document.createElement('script');
  s.type='application/json'; s.id='fw-measure';
  // Whether the page's own font actually arrived. A headless run with no
  // network gets a fallback, every line comes out wider, and boxes get
  // widened to compensate for a font that was never missing on the site.
  var fontOK={};
  try{ ['400 16px Raleway','500 16px Raleway','700 48px Raleway']
        .forEach(function(f){ fontOK[f]=document.fonts.check(f); }); }catch(e){}
  // Everything that only goes wrong on a phone, gathered at whatever width
  // this run was rendered at. There is no mobile frame in Figma for any of
  // these pages, so nothing else can answer "is the phone layout right" — and
  // the four questions below are the ones that were being retyped by hand
  // into the console, once per page, every time.
  var vw=window.innerWidth, mob={vw:vw,
      scrollW:document.documentElement.scrollWidth, over:[], small:[], tap:[],
      lefts:{}, imgs:[]};
  root.querySelectorAll('*').forEach(function(el){
    var r=el.getBoundingClientRect();
    if(!r.width) return;
    // A carousel clips its own off-screen items on purpose.
    if(!el.closest('.mc-loop') && (r.right>vw+1 || r.left<-1) && mob.over.length<20)
      mob.over.push({cls:cls(el), x:Math.round(r.left), r:Math.round(r.right)});
    var c=getComputedStyle(el);
    var ownText=[].slice.call(el.childNodes).some(function(n){
      return n.nodeType===3 && n.textContent.trim().length>2; });
    if(ownText && el.tagName!=='STYLE' && el.tagName!=='NOSCRIPT'){
      if(parseFloat(c.fontSize)<16 && mob.small.length<20)
        mob.small.push({cls:cls(el), fs:c.fontSize,
                        txt:el.textContent.trim().slice(0,26)});
      // Where does body copy start? A carousel's off-screen items are
      // parked off to the right and would otherwise dominate the tally.
      if(r.width>120 && !el.closest('.mc-loop')){ var k=Math.round(r.left);
        mob.lefts[k]=(mob.lefts[k]||0)+1; }
    }
    if((el.tagName==='A'||el.tagName==='BUTTON') && (r.height<40||r.width<40)
        && mob.tap.length<20)
      mob.tap.push({cls:cls(el), txt:el.textContent.trim().slice(0,18),
                    box:Math.round(r.width)+'x'+Math.round(r.height)});
    if(el.tagName==='IMG' && mob.imgs.length<24)
      mob.imgs.push({cls:cls(el), nat:el.naturalWidth+'x'+el.naturalHeight,
                     box:Math.round(r.width)+'x'+Math.round(r.height),
                     ratio:c.aspectRatio});
  });
  s.textContent=JSON.stringify({runs:out, sections:secs, boxes:boxes,
                                fontOK:fontOK, mobile:mob});
  document.body.appendChild(s);
  // Headless Chrome will not open a window narrower than 500px, so a real
  // phone width can only be had by rendering into an iframe of that width.
  // --dump-dom prints the top document only, so pass the payload up and let
  // the wrapper publish it under the same id.
  if (window.parent !== window) {
    try { window.parent.postMessage({fwMeasure: s.textContent}, '*'); } catch(e){}
  }
}
if (document.fonts && document.fonts.ready) {
  document.fonts.ready.then(fwMeasure);
} else {
  fwMeasure();
}

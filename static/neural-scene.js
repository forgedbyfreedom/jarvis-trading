/*!
 * FBF Neural Scene: anatomical point-cloud brain, firing neurons, drifting color fog.
 * Shared by the Jarvis Command Center, forgedbyfreedom.net and the FBF app (web).
 * Requires THREE (r128+) on window. Usage:
 *   const scene = FBFNeural.mount(canvas, {brainUrl: '/static/brain.bin'});
 *   scene.destroy();
 */
(function (root) {
  'use strict';

  function glowTex(THREE, stops) {
    const c = document.createElement('canvas'); c.width = c.height = 256;
    const g = c.getContext('2d'), gr = g.createRadialGradient(128, 128, 0, 128, 128, 128);
    stops.forEach(s => gr.addColorStop(s[0], s[1])); g.fillStyle = gr; g.fillRect(0, 0, 256, 256);
    return new THREE.CanvasTexture(c);
  }

  /* brain.bin: uint32 N, then N x (int16 x, int16 y, int16 z, uint8 groove, uint8 part) */
  function parseBrain(buf) {
    const dv = new DataView(buf), n = dv.getUint32(0, true), pts = new Float32Array(n * 3),
      groove = new Float32Array(n), part = new Uint8Array(n);
    for (let i = 0, o = 4; i < n; i++, o += 8) {
      pts[i * 3] = dv.getInt16(o, true) / 1e4; pts[i * 3 + 1] = dv.getInt16(o + 2, true) / 1e4;
      pts[i * 3 + 2] = dv.getInt16(o + 4, true) / 1e4; groove[i] = dv.getUint8(o + 6) / 255; part[i] = dv.getUint8(o + 7);
    }
    return { n, pts, groove, part };
  }

  function mount(canvas, opts) {
    opts = Object.assign({ brainUrl: '/static/brain.bin', scale: 1.35, fog: 1, neurons: 340,
      mouse: true, camZ: 6.2, offsetY: .1, quality: 1, thinkBoost: 4.5 }, opts || {});
    const THREE = root.THREE;
    if (!THREE) throw new Error('FBFNeural needs THREE');
    let alive = true, raf = 0;
    const R = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'high-performance' });
    R.setPixelRatio(Math.min(root.devicePixelRatio || 1, 2) * opts.quality);
    const PR = R.getPixelRatio();
    const S = new THREE.Scene();
    const cam = new THREE.PerspectiveCamera(45, 1, .1, 100); cam.position.set(0, .3, opts.camZ);

    /* ── color fog ── */
    const fog = new THREE.Group(); S.add(fog);
    const cols = [[0,212,255],[150,60,255],[255,40,170],[0,255,170],[255,190,0],[60,110,255],[255,90,40],[200,0,255],[0,170,255],[255,0,90]];
    const fogParts = [];
    for (let i = 0; i < 36; i++) {
      const c = cols[i % cols.length];
      const m = new THREE.SpriteMaterial({ map: glowTex(THREE, [[0, `rgba(${c},.85)`], [.3, `rgba(${c},.38)`], [.65, `rgba(${c},.1)`], [1, 'rgba(0,0,0,0)']]),
        transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, opacity: 0 });
      const sp = new THREE.Sprite(m);
      sp.userData = { r: 2.1 + Math.random() * 3.6, a: Math.random() * 6.283, y: (Math.random() - .5) * 3, z: -2.8 - Math.random() * 3.2,
        spd: (.04 + Math.random() * .08) * (Math.random() < .5 ? -1 : 1), sc: 3.2 + Math.random() * 4.8, ph: Math.random() * 6.28,
        op: (.14 + Math.random() * .2) * opts.fog };
      fog.add(sp); fogParts.push(sp);
    }
    const dN = 420, dP = new Float32Array(dN * 3);
    for (let i = 0; i < dN; i++) { dP[i * 3] = (Math.random() - .5) * 14; dP[i * 3 + 1] = (Math.random() - .5) * 7; dP[i * 3 + 2] = -4 + Math.random() * 6; }
    const dG = new THREE.BufferGeometry(); dG.setAttribute('position', new THREE.BufferAttribute(dP, 3));
    const dust = new THREE.Points(dG, new THREE.PointsMaterial({ color: 0x88ddff, size: .025, transparent: true, opacity: .45, depthWrite: false, blending: THREE.AdditiveBlending }));
    S.add(dust);

    /* ── shared glow-point shader ── */
    const VS = `attribute float size;attribute float alpha;attribute vec3 color;varying vec3 vC;varying float vA;uniform float pr;
      void main(){vC=color;vA=alpha;vec4 mv=modelViewMatrix*vec4(position,1.);gl_PointSize=size*pr*(6./-mv.z);gl_Position=projectionMatrix*mv;}`;
    const FS = `varying vec3 vC;varying float vA;void main(){float d=length(gl_PointCoord-.5);if(d>.5)discard;float g=pow(1.-d*2.,1.8);gl_FragColor=vec4(vC*(.6+g),g*vA);}`;
    const mat = () => new THREE.ShaderMaterial({ uniforms: { pr: { value: PR } }, vertexShader: VS, fragmentShader: FS,
      transparent: true, depthWrite: false, blending: THREE.AdditiveBlending });
    function geo(pos, col, size, alpha) {
      const g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.BufferAttribute(pos, 3)); g.setAttribute('color', new THREE.BufferAttribute(col, 3));
      g.setAttribute('size', new THREE.BufferAttribute(size, 1)); g.setAttribute('alpha', new THREE.BufferAttribute(alpha, 1)); return g;
    }

    const brain = new THREE.Group(); brain.scale.setScalar(opts.scale); S.add(brain);
    let nodes = [], edges = [], adj = [], NN = 0, eN = 0, eG, eCol, nG, nCol, nSize, nAlpha, nGlow, pG, pPos, pCol, pSize, pAlpha;
    const pulses = [], PMAX = 520;
    const fireCols = [0x00ffff, 0xff3cc8, 0xffd23f, 0x7cff6b, 0xb48cff].map(c => new THREE.Color(c));

    function build(b) {
      const { n, pts, groove, part } = b;
      const col = new Float32Array(n * 3), size = new Float32Array(n), alpha = new Float32Array(n);
      const cA = new THREE.Color(0x00d4ff), cB = new THREE.Color(0x9a5cff), cC = new THREE.Color(0x3cf0c8), t = new THREE.Color();
      for (let i = 0; i < n; i++) {
        const y = pts[i * 3 + 1], gyr = 1 - groove[i];
        if (part[i] === 2) t.copy(cC).lerp(cA, Math.random() * .4);
        else t.copy(cA).lerp(cB, Math.min(1, Math.max(0, (y + .55) / 1.3)) * .85 + Math.random() * .15);
        col[i * 3] = t.r; col[i * 3 + 1] = t.g; col[i * 3 + 2] = t.b;
        size[i] = .7 + gyr * 1.7 + Math.random() * .4;
        alpha[i] = .03 + gyr * gyr * .6;
      }
      brain.add(new THREE.Points(geo(pts, col, size, alpha), mat()));

      /* neurons on gyrus crests of the cortex */
      NN = opts.neurons; let guard = 0;
      while (nodes.length < NN && guard++ < 1e6) {
        const k = (Math.random() * n) | 0;
        if (part[k] > 1 || groove[k] > .05) continue;
        nodes.push(new THREE.Vector3(pts[k * 3] * .985, pts[k * 3 + 1] * .985, pts[k * 3 + 2] * .985));
      }
      NN = nodes.length; adj = nodes.map(() => []);
      for (let i = 0; i < NN; i++) {
        const near = [];
        for (let j = 0; j < NN; j++) if (j !== i) { const d = nodes[i].distanceTo(nodes[j]); if (d < .5) near.push([d, j]); }
        near.sort((a, c) => a[0] - c[0]).slice(0, 3).forEach(([, j]) => {
          if (adj[i].some(e => edges[e].a === j || edges[e].b === j)) return;
          const e = edges.length; edges.push({ a: i, b: j, glow: 0 }); adj[i].push(e); adj[j].push(e);
        });
      }
      eN = edges.length;
      const ePos = new Float32Array(eN * 6); eCol = new Float32Array(eN * 6);
      edges.forEach((e, k) => ePos.set([nodes[e.a].x, nodes[e.a].y, nodes[e.a].z, nodes[e.b].x, nodes[e.b].y, nodes[e.b].z], k * 6));
      eG = new THREE.BufferGeometry(); eG.setAttribute('position', new THREE.BufferAttribute(ePos, 3)); eG.setAttribute('color', new THREE.BufferAttribute(eCol, 3));
      brain.add(new THREE.LineSegments(eG, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: .9, depthWrite: false, blending: THREE.AdditiveBlending })));
      const nPos = new Float32Array(NN * 3); nCol = new Float32Array(NN * 3); nSize = new Float32Array(NN); nAlpha = new Float32Array(NN); nGlow = new Float32Array(NN);
      nodes.forEach((v, i) => nPos.set([v.x, v.y, v.z], i * 3));
      nG = geo(nPos, nCol, nSize, nAlpha); brain.add(new THREE.Points(nG, mat()));
      pPos = new Float32Array(PMAX * 3); pCol = new Float32Array(PMAX * 3); pSize = new Float32Array(PMAX); pAlpha = new Float32Array(PMAX);
      pG = geo(pPos, pCol, pSize, pAlpha); brain.add(new THREE.Points(pG, mat()));
    }
    function fire(n, col, depth) {
      nGlow[n] = 1;
      adj[n].forEach(e => {
        if (pulses.length < PMAX && Math.random() < (depth ? .6 : .95)) {
          const ed = edges[e]; pulses.push({ e, from: n, to: ed.a === n ? ed.b : ed.a, t: 0, spd: 1 + Math.random() * 1.5, col, depth });
        }
      });
    }

    fetch(opts.brainUrl).then(r => r.arrayBuffer()).then(buf => { if (alive) build(parseBrain(buf)); })
      .catch(e => console.warn('FBFNeural: brain data failed to load', e));

    let mx = 0, my = 0;
    const onMove = e => { mx = (e.clientX / innerWidth - .5) * .4; my = (e.clientY / innerHeight - .5) * -.25; };
    if (opts.mouse) document.addEventListener('mousemove', onMove);

    function resize() {
      const w = canvas.clientWidth, h = canvas.clientHeight;
      if (!w || !h) return;
      if (canvas.width !== Math.round(w * PR) || canvas.height !== Math.round(h * PR)) {
        R.setSize(w, h, false); cam.aspect = w / h; cam.updateProjectionMatrix();
      }
    }
    const ro = root.ResizeObserver ? new ResizeObserver(resize) : null;
    if (ro) ro.observe(canvas); else root.addEventListener('resize', resize);
    resize();

    /* "thinking" mode: while a question is out, firing runs at 4x+ and the cloud brightens */
    let think = 1, thinkTarget = 1, thinkers = 0;
    function setThinking(on) { thinkers = Math.max(0, thinkers + (on ? 1 : -1)); thinkTarget = thinkers > 0 ? opts.thinkBoost : 1; }
    const onThink = e => setThinking(!!(e.detail && e.detail.on));
    root.addEventListener('fbf:thinking', onThink);

    const ck = new THREE.Clock(); let last = 0, nextFire = 0;
    (function anim() {
      if (!alive) return;
      raf = requestAnimationFrame(anim);
      if (document.hidden) return;
      const t = ck.getElapsedTime(), dt = Math.min(t - last, .05); last = t;
      brain.rotation.y = t * .16; brain.rotation.x = -.1 + Math.sin(t * .35) * .05; brain.position.y = opts.offsetY + Math.sin(t * .7) * .07;
      think += (thinkTarget - think) * Math.min(1, dt * (thinkTarget > think ? 4 : 1.2));
      const boost = (think - 1) / Math.max(.001, opts.thinkBoost - 1);   /* 0 idle .. 1 full thinking */
      if (NN) {
        if (t > nextFire) {
          const bursts = Math.round(think);
          for (let b = 0; b < bursts; b++) fire((Math.random() * NN) | 0, fireCols[(Math.random() * fireCols.length) | 0], 0);
          nextFire = t + (.1 + Math.random() * .3) / think;
        }
        for (let i = pulses.length - 1; i >= 0; i--) {
          const p = pulses[i]; p.t += dt * p.spd * (1 + boost * 1.2); edges[p.e].glow = 1;
          if (p.t >= 1) { pulses.splice(i, 1); if (p.depth < 3) fire(p.to, p.col, p.depth + 1); else nGlow[p.to] = 1; }
        }
        for (let i = 0; i < PMAX; i++) {
          const p = pulses[i];
          if (p) { const A = nodes[p.from], B = nodes[p.to];
            pPos[i * 3] = A.x + (B.x - A.x) * p.t; pPos[i * 3 + 1] = A.y + (B.y - A.y) * p.t; pPos[i * 3 + 2] = A.z + (B.z - A.z) * p.t;
            pCol[i * 3] = p.col.r; pCol[i * 3 + 1] = p.col.g; pCol[i * 3 + 2] = p.col.b; pSize[i] = 7; pAlpha[i] = 1;
          } else { pSize[i] = 0; pAlpha[i] = 0; }
        }
        const a = pG.attributes; a.position.needsUpdate = a.color.needsUpdate = a.size.needsUpdate = a.alpha.needsUpdate = true;
        for (let i = 0; i < NN; i++) { nGlow[i] *= .93; const g = nGlow[i]; nCol[i * 3] = .2 + g * .8; nCol[i * 3 + 1] = .75 + g * .25; nCol[i * 3 + 2] = 1; nSize[i] = 2.6 + boost * 1.6 + g * (9 + boost * 5); nAlpha[i] = .3 + boost * .35 + g * .7; }
        const b = nG.attributes; b.color.needsUpdate = b.size.needsUpdate = b.alpha.needsUpdate = true;
        for (let k = 0; k < eN; k++) { const e = edges[k]; e.glow *= .95; const v = .04 + boost * .06 + e.glow * (.75 + boost * .25);
          for (let q = 0; q < 2; q++) { eCol[k * 6 + q * 3] = v * .3; eCol[k * 6 + q * 3 + 1] = v * .85; eCol[k * 6 + q * 3 + 2] = v; } }
        eG.attributes.color.needsUpdate = true;
      }
      fogParts.forEach(sp => { const u = sp.userData; u.a += u.spd * dt;
        sp.position.set(Math.cos(u.a) * u.r, u.y + Math.sin(t * .25 + u.ph) * .4, u.z);
        sp.material.opacity = u.op * (.75 + .25 * Math.sin(t * .5 + u.ph)) * (1 + boost * .7); sp.material.rotation += dt * .05;
        sp.scale.setScalar(u.sc * (1 + .08 * Math.sin(t * .3 + u.ph))); });
      fog.rotation.z = Math.sin(t * .05) * .2; dust.rotation.y = t * .02; dust.position.y = Math.sin(t * .2) * .15;
      cam.position.x = mx * 1.6; cam.position.y = .3 + my; cam.lookAt(0, 0, 0);
      resize(); R.render(S, cam);
    })();

    return {
      setThinking,
      destroy() {
        alive = false; cancelAnimationFrame(raf); root.removeEventListener('fbf:thinking', onThink);
        if (opts.mouse) document.removeEventListener('mousemove', onMove);
        if (ro) ro.disconnect();
        R.dispose();
      }
    };
  }

  /* Any page can light the brain up:  FBFNeural.thinking(true) ... FBFNeural.thinking(false) */
  function thinking(on) { root.dispatchEvent(new CustomEvent('fbf:thinking', { detail: { on: !!on } })); }
  root.FBFNeural = { mount, parseBrain, thinking };
})(typeof window !== 'undefined' ? window : this);

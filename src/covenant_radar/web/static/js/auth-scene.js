"use strict";

/* The sign-in hero: a real 3D scene, drawn with raw WebGL.
 *
 * Why not three.js: the application's Content-Security-Policy is
 * `script-src 'self'` with no external origin (see
 * `covenant_radar/security/headers.py`), so any library has to be vendored
 * into `static/vendor/`. three.js is ~600 KB of unreviewed code to carry for
 * one decorative surface; the scene below is the same idea in one file that
 * uses only the platform's own WebGL context.
 *
 * The scene is the product's argument: a covenant surface that deforms under
 * pressure, a sweep that discloses it, and the exposures the sweep lights up.
 * It is decoration — `aria-hidden` — and every failure path (no WebGL, a lost
 * context, reduced motion, a hidden tab) degrades to the CSS orbit that the
 * markup already ships.
 */

(() => {
  const canvas = document.querySelector("[data-auth-canvas]");
  if (!canvas) {
    return;
  }
  const story = canvas.closest(".auth-story") || canvas.parentElement;

  const TAU = Math.PI * 2;
  const GRID_X = 13.0;
  const GRID_BACK = -34.0;
  const GRID_FRONT = 5.5;
  const COLS = 54;
  const ROWS = 80;
  const POINT_COUNT = 132;
  const RING_SEGMENTS = 96;
  const RING_RADII = [1.55, 2.9, 4.35, 5.8];
  const BEAM_SEGMENTS = 30;
  const BEAM_SPAN = 0.62;
  /* Fog is what sets the horizon: the grid stops being drawn where fog
   * reaches zero, so a short FOG_FAR crops the surface into a band halfway up
   * the panel instead of letting it converge. */
  const FOG_NEAR = 3.0;
  const FOG_FAR = 34.0;

  /* One height field, shared verbatim by every program that has to agree on
   * where the surface is. Points floating "above the terrain" only read as 3D
   * while they are measured against the same function the mesh uses. */
  const TERRAIN = `
    float terrain(vec2 p, float t) {
      return sin(p.x * 0.42 + t * 0.55) * 0.42
           + sin(p.y * 0.31 - t * 0.42) * 0.50
           + sin((p.x + p.y) * 0.22 + t * 0.31) * 0.31;
    }
  `;

  const SWEEP_GLOW = `
    float sweepGlow(vec3 pos, float sweep, float falloff) {
      float ang = atan(pos.z, pos.x + 0.0001);
      float behind = mod(sweep - ang, 6.2831853);
      return exp(-behind * falloff);
    }
  `;

  const MESH_VERT = `
    precision highp float;
    attribute vec2 aGrid;
    uniform mat4 uViewProj;
    uniform mat4 uView;
    uniform float uTime;
    uniform float uSweep;
    uniform float uFlatten;
    varying float vDepth;
    varying float vLift;
    varying float vGlow;
    ${TERRAIN}
    ${SWEEP_GLOW}
    void main() {
      float h = terrain(aGrid, uTime) * uFlatten;
      vec3 pos = vec3(aGrid.x, h, aGrid.y);
      vec4 view = uView * vec4(pos, 1.0);
      vDepth = -view.z;
      vLift = clamp(h * 1.9 + 0.5, 0.0, 1.0);
      vGlow = sweepGlow(pos, uSweep, 2.6) * smoothstep(8.2, 1.2, length(aGrid));
      gl_Position = uViewProj * vec4(pos, 1.0);
    }
  `;

  const MESH_FRAG = `
    precision mediump float;
    uniform vec3 uInk;
    uniform vec3 uAccent;
    uniform vec2 uFog;
    uniform float uOpacity;
    varying float vDepth;
    varying float vLift;
    varying float vGlow;
    void main() {
      float fog = clamp(1.0 - (vDepth - uFog.x) / (uFog.y - uFog.x), 0.0, 1.0);
      fog = pow(fog, 1.4);
      /* Pull the nearest rows down as well. They fall across the bottom of
       * the panel, which is where the headline sits. */
      fog *= mix(0.34, 1.0, smoothstep(4.0, 8.5, vDepth));
      float warm = clamp(vLift * 0.45 + vGlow * 1.15, 0.0, 1.0);
      vec3 color = mix(uInk, uAccent, warm);
      float alpha = uOpacity * fog * (0.34 + 0.66 * vLift) + vGlow * 0.75 * fog;
      gl_FragColor = vec4(color, clamp(alpha, 0.0, 1.0));
    }
  `;

  const POINT_VERT = `
    precision highp float;
    attribute vec4 aSeed;
    uniform mat4 uViewProj;
    uniform mat4 uView;
    uniform float uTime;
    uniform float uSweep;
    uniform float uScale;
    varying float vDepth;
    varying float vGlow;
    varying float vRank;
    ${TERRAIN}
    ${SWEEP_GLOW}
    void main() {
      vec2 g = vec2(
        aSeed.x + sin(uTime * 0.21 + aSeed.z) * 0.36,
        aSeed.y + cos(uTime * 0.17 + aSeed.z) * 0.36
      );
      /* Squared tier: most exposures hug the surface, a few ride well above
       * it, which is what puts motes in the empty sky over the horizon. */
      float tier = fract(aSeed.z * 0.61);
      float hover = 0.18 + 0.40 * (0.5 + 0.5 * sin(uTime * 0.5 + aSeed.z * 2.3))
                  + tier * tier * 2.8;
      vec3 pos = vec3(g.x, terrain(g, uTime) + hover, g.y);
      vec4 view = uView * vec4(pos, 1.0);
      vDepth = -view.z;
      vGlow = sweepGlow(pos, uSweep, 2.1);
      vRank = aSeed.w;
      gl_PointSize = clamp(aSeed.w * uScale / max(vDepth, 0.5), 1.5, 46.0);
      gl_Position = uViewProj * vec4(pos, 1.0);
    }
  `;

  const POINT_FRAG = `
    precision mediump float;
    uniform vec3 uAccent;
    uniform vec3 uInk;
    uniform vec2 uFog;
    uniform float uOpacity;
    varying float vDepth;
    varying float vGlow;
    varying float vRank;
    void main() {
      float dist = length(gl_PointCoord - 0.5);
      if (dist > 0.5) {
        discard;
      }
      float core = smoothstep(0.5, 0.06, dist);
      float halo = smoothstep(0.5, 0.14, dist);
      float fog = clamp(1.0 - (vDepth - uFog.x) / (uFog.y - uFog.x), 0.0, 1.0);
      fog = pow(fog, 1.2);
      vec3 color = mix(uInk, uAccent, clamp(0.35 + vGlow * 1.2, 0.0, 1.0));
      float alpha = uOpacity * fog * (core * (0.52 + 1.35 * vGlow) + halo * 0.30);
      gl_FragColor = vec4(color, clamp(alpha, 0.0, 1.0));
    }
  `;

  const BEAM_VERT = `
    precision highp float;
    attribute vec3 aWedge;
    uniform mat4 uViewProj;
    uniform mat4 uView;
    uniform float uTime;
    uniform float uSweep;
    uniform float uRadius;
    varying float vDepth;
    varying float vRadial;
    varying float vTrail;
    ${TERRAIN}
    void main() {
      float ang = uSweep + aWedge.y;
      float r = aWedge.x * uRadius;
      vec2 g = vec2(cos(ang) * r, sin(ang) * r);
      vec3 pos = vec3(g.x, terrain(g, uTime) * 0.42 + 0.02, g.y);
      vec4 view = uView * vec4(pos, 1.0);
      vDepth = -view.z;
      vRadial = 1.0 - aWedge.x;
      vTrail = aWedge.z;
      gl_Position = uViewProj * vec4(pos, 1.0);
    }
  `;

  const BEAM_FRAG = `
    precision mediump float;
    uniform vec3 uAccent;
    uniform vec2 uFog;
    uniform float uOpacity;
    varying float vDepth;
    varying float vRadial;
    varying float vTrail;
    void main() {
      float fog = clamp(1.0 - (vDepth - uFog.x) / (uFog.y - uFog.x), 0.0, 1.0);
      float alpha = uOpacity * fog * pow(vTrail, 3.0) * (0.10 + 0.90 * pow(vRadial, 0.8));
      gl_FragColor = vec4(uAccent, clamp(alpha, 0.0, 1.0));
    }
  `;

  /* ---- linear algebra ---------------------------------------------- */

  function perspective(fovy, aspect, near, far) {
    const f = 1 / Math.tan(fovy / 2);
    const nf = 1 / (near - far);
    return new Float32Array([
      f / aspect, 0, 0, 0,
      0, f, 0, 0,
      0, 0, (far + near) * nf, -1,
      0, 0, 2 * far * near * nf, 0,
    ]);
  }

  function lookAt(eye, target) {
    let z0 = eye[0] - target[0];
    let z1 = eye[1] - target[1];
    let z2 = eye[2] - target[2];
    let len = Math.hypot(z0, z1, z2) || 1;
    z0 /= len;
    z1 /= len;
    z2 /= len;
    let x0 = z2;
    let x1 = 0;
    let x2 = -z0;
    len = Math.hypot(x0, x1, x2) || 1;
    x0 /= len;
    x1 /= len;
    x2 /= len;
    const y0 = z1 * x2 - z2 * x1;
    const y1 = z2 * x0 - z0 * x2;
    const y2 = z0 * x1 - z1 * x0;
    return new Float32Array([
      x0, y0, z0, 0,
      x1, y1, z1, 0,
      x2, y2, z2, 0,
      -(x0 * eye[0] + x1 * eye[1] + x2 * eye[2]),
      -(y0 * eye[0] + y1 * eye[1] + y2 * eye[2]),
      -(z0 * eye[0] + z1 * eye[1] + z2 * eye[2]),
      1,
    ]);
  }

  function multiply(a, b) {
    const out = new Float32Array(16);
    for (let column = 0; column < 4; column += 1) {
      const b0 = b[column * 4];
      const b1 = b[column * 4 + 1];
      const b2 = b[column * 4 + 2];
      const b3 = b[column * 4 + 3];
      for (let row = 0; row < 4; row += 1) {
        out[column * 4 + row] =
          a[row] * b0 + a[4 + row] * b1 + a[8 + row] * b2 + a[12 + row] * b3;
      }
    }
    return out;
  }

  /* ---- palette ------------------------------------------------------ */

  /* The stage owns its own palette (`--auth-scene-*` in auth.css) because it
   * is dark in both themes; the global accent tokens are the fallback so the
   * scene still draws if that stylesheet ever stops loading. */
  function readColor(names, fallback) {
    const style = getComputedStyle(story || document.documentElement);
    for (let index = 0; index < names.length; index += 1) {
      const parsed = parseColor(style.getPropertyValue(names[index]).trim());
      if (parsed) {
        return parsed;
      }
    }
    return fallback;
  }

  function parseColor(value) {
    if (!value) {
      return null;
    }
    const hex = value.replace("#", "");
    if (/^[0-9a-f]{3}$/i.test(hex)) {
      return [
        parseInt(hex[0] + hex[0], 16) / 255,
        parseInt(hex[1] + hex[1], 16) / 255,
        parseInt(hex[2] + hex[2], 16) / 255,
      ];
    }
    if (/^[0-9a-f]{6}$/i.test(hex)) {
      return [
        parseInt(hex.slice(0, 2), 16) / 255,
        parseInt(hex.slice(2, 4), 16) / 255,
        parseInt(hex.slice(4, 6), 16) / 255,
      ];
    }
    const numeric = value.match(/-?[\d.]+/g);
    if (numeric && numeric.length >= 3) {
      return [
        Number(numeric[0]) / 255,
        Number(numeric[1]) / 255,
        Number(numeric[2]) / 255,
      ];
    }
    return null;
  }

  /* ---- geometry ------------------------------------------------------ */

  function buildGrid() {
    const vertices = [];
    const stepX = (GRID_X * 2) / (COLS - 1);
    const stepZ = (GRID_FRONT - GRID_BACK) / (ROWS - 1);
    for (let column = 0; column < COLS; column += 1) {
      const x = -GRID_X + column * stepX;
      for (let row = 0; row < ROWS - 1; row += 1) {
        vertices.push(x, GRID_BACK + row * stepZ, x, GRID_BACK + (row + 1) * stepZ);
      }
    }
    for (let row = 0; row < ROWS; row += 1) {
      const z = GRID_BACK + row * stepZ;
      for (let column = 0; column < COLS - 1; column += 1) {
        vertices.push(-GRID_X + column * stepX, z, -GRID_X + (column + 1) * stepX, z);
      }
    }
    return new Float32Array(vertices);
  }

  function buildRings() {
    const vertices = [];
    RING_RADII.forEach((radius) => {
      for (let index = 0; index < RING_SEGMENTS; index += 1) {
        const angle = (index / RING_SEGMENTS) * TAU;
        vertices.push(Math.cos(angle) * radius, Math.sin(angle) * radius);
      }
    });
    return new Float32Array(vertices);
  }

  function buildPoints() {
    const vertices = new Float32Array(POINT_COUNT * 4);
    /* A fixed sequence, not Math.random: the same composition renders on
     * every visit and on every reload, so the page has a look rather than a
     * roll of the dice. */
    let seed = 0x9e3779b9;
    const next = () => {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      return seed / 4294967296;
    };
    for (let index = 0; index < POINT_COUNT; index += 1) {
      const angle = next() * TAU;
      const radius = 0.9 + Math.sqrt(next()) * 6.4;
      vertices[index * 4] = Math.cos(angle) * radius;
      vertices[index * 4 + 1] = Math.sin(angle) * radius - 2.2;
      vertices[index * 4 + 2] = next() * TAU;
      vertices[index * 4 + 3] = 5.0 + next() * 9.0;
    }
    return vertices;
  }

  function buildBeam() {
    const vertices = [0, 0, 1];
    for (let index = 0; index <= BEAM_SEGMENTS; index += 1) {
      const ratio = index / BEAM_SEGMENTS;
      vertices.push(1, -BEAM_SPAN * (1 - ratio), ratio);
    }
    return new Float32Array(vertices);
  }

  /* ---- gl plumbing --------------------------------------------------- */

  const options = {
    alpha: true,
    antialias: true,
    depth: false,
    premultipliedAlpha: false,
    powerPreference: "low-power",
  };
  const gl =
    canvas.getContext("webgl", options) ||
    canvas.getContext("experimental-webgl", options);
  if (!gl) {
    return;
  }

  function compile(type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      gl.deleteShader(shader);
      return null;
    }
    return shader;
  }

  function link(vertexSource, fragmentSource, attributes, uniforms) {
    const vertex = compile(gl.VERTEX_SHADER, vertexSource);
    const fragment = compile(gl.FRAGMENT_SHADER, fragmentSource);
    if (!vertex || !fragment) {
      return null;
    }
    const handle = gl.createProgram();
    gl.attachShader(handle, vertex);
    gl.attachShader(handle, fragment);
    gl.linkProgram(handle);
    gl.deleteShader(vertex);
    gl.deleteShader(fragment);
    if (!gl.getProgramParameter(handle, gl.LINK_STATUS)) {
      gl.deleteProgram(handle);
      return null;
    }
    const bundle = { handle, attributes: {}, uniforms: {} };
    attributes.forEach((name) => {
      bundle.attributes[name] = gl.getAttribLocation(handle, name);
    });
    uniforms.forEach((name) => {
      bundle.uniforms[name] = gl.getUniformLocation(handle, name);
    });
    return bundle;
  }

  const meshProgram = link(
    MESH_VERT,
    MESH_FRAG,
    ["aGrid"],
    ["uViewProj", "uView", "uTime", "uSweep", "uFlatten", "uInk", "uAccent", "uFog", "uOpacity"],
  );
  const pointProgram = link(
    POINT_VERT,
    POINT_FRAG,
    ["aSeed"],
    ["uViewProj", "uView", "uTime", "uSweep", "uScale", "uInk", "uAccent", "uFog", "uOpacity"],
  );
  const beamProgram = link(
    BEAM_VERT,
    BEAM_FRAG,
    ["aWedge"],
    ["uViewProj", "uView", "uTime", "uSweep", "uRadius", "uAccent", "uFog", "uOpacity"],
  );
  if (!meshProgram || !pointProgram || !beamProgram) {
    return;
  }

  function upload(data) {
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
    return buffer;
  }

  const gridData = buildGrid();
  const gridBuffer = upload(gridData);
  const ringBuffer = upload(buildRings());
  const pointBuffer = upload(buildPoints());
  const beamBuffer = upload(buildBeam());
  const gridVertexCount = gridData.length / 2;

  gl.disable(gl.DEPTH_TEST);
  gl.enable(gl.BLEND);
  /* Additive, not source-over. A 1px line at devicePixelRatio 2 covers half a
   * CSS pixel, so alpha compositing can never make the mesh read; adding
   * light instead lets crossings and the swept arc accumulate into a glow.
   * This is only safe because the stage is dark in both themes. */
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE);

  /* ---- state --------------------------------------------------------- */

  const motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  const ACCENT_TOKENS = ["--auth-scene-accent", "--accent"];
  const INK_TOKENS = ["--auth-scene-ink", "--ink-muted"];
  let accent = readColor(ACCENT_TOKENS, [0.96, 0.66, 0.86]);
  let ink = readColor(INK_TOKENS, [0.49, 0.36, 0.48]);
  let projection = perspective(Math.PI / 4, 1, 0.1, 60);
  let pixelRatio = 1;
  let width = 1;
  let height = 1;
  let frame = 0;
  let startedAt = 0;
  let elapsed = 0;
  let parallaxX = 0;
  let parallaxY = 0;
  let targetX = 0;
  let targetY = 0;

  function refreshPalette() {
    accent = readColor(ACCENT_TOKENS, accent);
    ink = readColor(INK_TOKENS, ink);
  }

  function resize() {
    const rect = canvas.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1) {
      return false;
    }
    pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
    const nextWidth = Math.round(rect.width * pixelRatio);
    const nextHeight = Math.round(rect.height * pixelRatio);
    if (nextWidth !== width || nextHeight !== height) {
      width = nextWidth;
      height = nextHeight;
      canvas.width = width;
      canvas.height = height;
      projection = perspective(Math.PI / 4, width / height, 0.1, 60);
    }
    return true;
  }

  function bindAttribute(location, buffer, size) {
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.enableVertexAttribArray(location);
    gl.vertexAttribPointer(location, size, gl.FLOAT, false, 0, 0);
  }

  function draw(time) {
    const sweep = -time * 0.42;
    /* ~15 degrees of downward pitch. That puts the horizon a sixth of the way
     * down the panel and the sweep origin near the middle, which leaves the
     * bottom third — where the headline sits — reading as depth rather than
     * as wireframe behind type. */
    const eye = [parallaxX * 0.9, 2.7 + parallaxY * 0.5, 7.4];
    const view = lookAt(eye, [parallaxX * 0.35, 0.55, 0.2]);
    const viewProj = multiply(projection, view);

    gl.viewport(0, 0, width, height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);

    /* The wedge goes down first so the mesh and the exposures read on top of
     * it rather than being washed out by it. */
    gl.useProgram(beamProgram.handle);
    bindAttribute(beamProgram.attributes.aWedge, beamBuffer, 3);
    gl.uniformMatrix4fv(beamProgram.uniforms.uViewProj, false, viewProj);
    gl.uniformMatrix4fv(beamProgram.uniforms.uView, false, view);
    gl.uniform1f(beamProgram.uniforms.uTime, time);
    gl.uniform1f(beamProgram.uniforms.uSweep, sweep);
    gl.uniform1f(beamProgram.uniforms.uRadius, 6.2);
    gl.uniform3fv(beamProgram.uniforms.uAccent, accent);
    gl.uniform2f(beamProgram.uniforms.uFog, FOG_NEAR, FOG_FAR);
    gl.uniform1f(beamProgram.uniforms.uOpacity, 0.10);
    gl.drawArrays(gl.TRIANGLE_FAN, 0, BEAM_SEGMENTS + 2);

    gl.useProgram(meshProgram.handle);
    gl.uniformMatrix4fv(meshProgram.uniforms.uViewProj, false, viewProj);
    gl.uniformMatrix4fv(meshProgram.uniforms.uView, false, view);
    gl.uniform1f(meshProgram.uniforms.uTime, time);
    gl.uniform1f(meshProgram.uniforms.uSweep, sweep);
    gl.uniform3fv(meshProgram.uniforms.uInk, ink);
    gl.uniform3fv(meshProgram.uniforms.uAccent, accent);
    gl.uniform2f(meshProgram.uniforms.uFog, FOG_NEAR, FOG_FAR);

    bindAttribute(meshProgram.attributes.aGrid, gridBuffer, 2);
    gl.uniform1f(meshProgram.uniforms.uFlatten, 1);
    gl.uniform1f(meshProgram.uniforms.uOpacity, 1.05);
    gl.drawArrays(gl.LINES, 0, gridVertexCount);

    bindAttribute(meshProgram.attributes.aGrid, ringBuffer, 2);
    gl.uniform1f(meshProgram.uniforms.uFlatten, 0.4);
    gl.uniform1f(meshProgram.uniforms.uOpacity, 1.6);
    RING_RADII.forEach((_radius, index) => {
      gl.drawArrays(gl.LINE_LOOP, index * RING_SEGMENTS, RING_SEGMENTS);
    });

    gl.useProgram(pointProgram.handle);
    bindAttribute(pointProgram.attributes.aSeed, pointBuffer, 4);
    gl.uniformMatrix4fv(pointProgram.uniforms.uViewProj, false, viewProj);
    gl.uniformMatrix4fv(pointProgram.uniforms.uView, false, view);
    gl.uniform1f(pointProgram.uniforms.uTime, time);
    gl.uniform1f(pointProgram.uniforms.uSweep, sweep);
    gl.uniform1f(pointProgram.uniforms.uScale, 5.4 * pixelRatio);
    gl.uniform3fv(pointProgram.uniforms.uInk, ink);
    gl.uniform3fv(pointProgram.uniforms.uAccent, accent);
    gl.uniform2f(pointProgram.uniforms.uFog, FOG_NEAR, FOG_FAR);
    gl.uniform1f(pointProgram.uniforms.uOpacity, 1);
    gl.drawArrays(gl.POINTS, 0, POINT_COUNT);
  }

  function renderStill() {
    if (resize()) {
      draw(6.4);
    }
  }

  function tick(now) {
    frame = window.requestAnimationFrame(tick);
    if (!startedAt) {
      startedAt = now;
    }
    if (!resize()) {
      return;
    }
    elapsed = (now - startedAt) / 1000;
    parallaxX += (targetX - parallaxX) * 0.045;
    parallaxY += (targetY - parallaxY) * 0.045;
    draw(elapsed);
  }

  function start() {
    if (frame || motionQuery.matches) {
      return;
    }
    startedAt = 0;
    frame = window.requestAnimationFrame(tick);
  }

  function stop() {
    if (frame) {
      window.cancelAnimationFrame(frame);
      frame = 0;
    }
  }

  /* ---- wiring -------------------------------------------------------- */

  if (story) {
    story.dataset.canvas = "live";
  }

  if (motionQuery.matches) {
    renderStill();
  } else {
    start();
  }

  const onMotionChange = () => {
    stop();
    if (motionQuery.matches) {
      renderStill();
    } else {
      start();
    }
  };
  if (typeof motionQuery.addEventListener === "function") {
    motionQuery.addEventListener("change", onMotionChange);
  } else if (typeof motionQuery.addListener === "function") {
    motionQuery.addListener(onMotionChange);
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stop();
    } else if (!motionQuery.matches) {
      start();
    }
  });

  window.addEventListener("resize", () => {
    if (motionQuery.matches) {
      renderStill();
    }
  });

  /* Parallax is what sells the depth: the camera, not the geometry, moves. */
  window.addEventListener(
    "pointermove",
    (event) => {
      if (event.pointerType === "touch") {
        return;
      }
      targetX = (event.clientX / window.innerWidth - 0.5) * 1.5;
      targetY = (0.5 - event.clientY / window.innerHeight) * 1.1;
    },
    { passive: true },
  );

  canvas.addEventListener("webglcontextlost", (event) => {
    event.preventDefault();
    stop();
    if (story) {
      story.dataset.canvas = "lost";
    }
  });

  const themeObserver = new MutationObserver(() => {
    refreshPalette();
    if (motionQuery.matches) {
      renderStill();
    }
  });
  themeObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
})();

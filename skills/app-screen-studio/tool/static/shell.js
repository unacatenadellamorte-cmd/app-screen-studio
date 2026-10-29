/* アプリ画面編集室: 編集パネル側(shell)。画面本体はiframeで同一オリジン表示する。 */
(function () {
  'use strict';
  var PREFIX = '/__screen_studio__';
  var SIDES = ['Top', 'Right', 'Bottom', 'Left'];
  var GROUPS = {
    radius: ['borderTopLeftRadius', 'borderTopRightRadius', 'borderBottomRightRadius', 'borderBottomLeftRadius'],
    padding: SIDES.map(function (s) { return 'padding' + s; }),
    margin: SIDES.map(function (s) { return 'margin' + s; }),
    borderWidth: SIDES.map(function (s) { return 'border' + s + 'Width'; }),
    borderStyle: SIDES.map(function (s) { return 'border' + s + 'Style'; }),
    borderColor: SIDES.map(function (s) { return 'border' + s + 'Color'; })
  };
  var VIEWPORTS = { phone: [390, 844], tablet: [820, 1180], pc: [1280, 800] };
  var FONTS = ['system-ui, sans-serif', '"Yu Gothic UI", "Hiragino Sans", "Meiryo", sans-serif',
    '"Yu Mincho", "Hiragino Mincho ProN", serif', 'ui-monospace, Consolas, monospace', 'Arial, Helvetica, sans-serif'];

  var $ = function (id) { return document.getElementById(id); };
  var frame = $('frame'), session = null, vp = 'phone', mode = 'edit';
  var state = { dirty: false, canUndo: false, canRedo: false, count: 0 };
  var sel = null, selCount = 0, screen = { path: '', title: '' }, lastSave = null, pendingCollect = {}, collectSeq = 0;
  var toastTimer = null;

  // ------------------------------------------------------------ 通信
  function send(msg) { if (frame.contentWindow) frame.contentWindow.postMessage(msg, location.origin); }
  function toast(text) {
    var t = $('toast'); t.textContent = text; t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { t.hidden = true; }, 3500);
  }
  function api(path, opts) {
    return fetch(PREFIX + path, opts).then(function (r) { return r.json().catch(function () { return { ok: false, message: 'HTTP ' + r.status }; }); });
  }

  window.addEventListener('message', function (e) {
    if (e.source !== frame.contentWindow || e.origin !== location.origin) return;
    var m = e.data;
    if (!m || typeof m.ss !== 'string') return;
    if (m.ss === 'ready') onReady(m);
    else if (m.ss === 'state') { state = m; renderStatus(); }
    else if (m.ss === 'selection') { sel = m.info; selCount = m.count || 0; renderPanel(); }
    else if (m.ss === 'toast') toast(m.text);
    else if (m.ss === 'edits') { var cb = pendingCollect[m.id]; delete pendingCollect[m.id]; if (cb) cb(m); }
    else if (m.ss === 'reapplied') onReapplied(m);
  });

  // ------------------------------------------------------------ 起動
  function init() {
    var q = new URLSearchParams(location.search);
    var path = q.get('path') || '/';
    if (path.charAt(0) !== '/' || path.charAt(1) === '/' || path.indexOf('\\') !== -1) path = '/';
    var dl = $('fontList');
    FONTS.forEach(function (f) { var o = document.createElement('option'); o.value = f; dl.appendChild(o); });
    api('/api/session').then(function (s) {
      if (!s.ok) { toast('セッションを取得できません'); return; }
      session = s;
      var caps = s.capabilities || {};
      $('fsMaterial').disabled = !caps.material_colors;
      $('fsGlow').disabled = !caps.text_glow;
      if (!caps.material_colors) $('materialStatus').textContent = '旧版の起動です。AIに画面スキルの更新を依頼すると、材質と発光を保存できるようになります。';
      $('projectName').textContent = s.project;
      frame.src = path;
      if (typeof showAiConfig === 'function') { showAiConfig(s.ai || { configured: false, missing: ['不明'] }); if (s.ai && s.ai.configured) refreshAiStatus(); }
    });
    setViewport('phone');
    setMode('edit');
    renderStatus();
  }

  var currentPath = '';
  function onReady(m) {
    screen = { path: m.path, title: m.title };
    $('screenPath').textContent = '画面: ' + m.path + (m.title ? '(' + m.title + ')' : '');
    sel = null; renderPanel();
    send({ ss: 'mode', mode: mode });
    if (m.path !== currentPath) { currentPath = m.path; checkSaved(); }
    if (history.replaceState) history.replaceState(null, '', PREFIX + '/shell?path=' + encodeURIComponent(m.path));
  }

  // ------------------------------------------------------------ 保存済みの再適用
  var savedEdits = null;
  function checkSaved() {
    if (!session) return;
    var requestedPath = screen.path;
    api('/api/load?path=' + encodeURIComponent(requestedPath), { headers: { 'X-Studio-Token': session.token } }).then(function (r) {
      if (screen.path !== requestedPath) return;
      var n = $('notice');
      if (r.ok && r.exists && r.data.edits.length) {
        savedEdits = r.data.edits;
        n.hidden = false; n.textContent = '';
        n.appendChild(document.createTextNode('この画面の保存済みの編集が ' + savedEdits.length + ' 件あります(製品コードへは未反映の記録です)。 '));
        var b = document.createElement('button'); b.className = 'btn small'; b.textContent = '画面に再適用';
        b.addEventListener('click', function () { send({ ss: 'reapply', edits: savedEdits }); n.hidden = true; });
        n.appendChild(b);
      } else { n.hidden = true; savedEdits = null; }
    });
  }
  function onReapplied(m) {
    var msg = m.applied + '件を再適用';
    if (m.already) msg += '、' + m.already + '件は適用済み';
    if (m.skipped.length) msg += '、' + m.skipped.length + '件は元の要素と一致せず見送り(' + m.skipped.slice(0, 2).map(function (s) { return s.target + ': ' + s.reason; }).join(' / ') + ')';
    toast(msg);
  }

  // ------------------------------------------------------------ 画面サイズ・モード
  function setViewport(name) {
    vp = name;
    var wh = VIEWPORTS[name];
    frame.style.width = wh[0] + 'px'; frame.style.height = wh[1] + 'px';
    Array.prototype.forEach.call(document.querySelectorAll('.vp'), function (b) { b.classList.toggle('on', b.dataset.vp === name); });
    fit();
  }
  function fit() {
    var wh = VIEWPORTS[vp], wrap = $('frameWrap'), stage = $('stage');
    var avail = stage.clientWidth - 26;
    var scale = Math.min(1, avail / wh[0]);
    frame.style.transform = scale < 1 ? 'scale(' + scale + ')' : 'none';
    wrap.style.width = Math.round(wh[0] * scale) + 'px'; wrap.style.height = Math.round(wh[1] * scale) + 'px';
  }
  window.addEventListener('resize', fit);
  function setMode(m) {
    mode = m;
    if (m !== 'edit') finishRange(true);
    Array.prototype.forEach.call(document.querySelectorAll('.mode'), function (b) { b.classList.toggle('on', b.dataset.mode === m); });
    send({ ss: 'mode', mode: m });
  }
  Array.prototype.forEach.call(document.querySelectorAll('.vp'), function (b) { b.addEventListener('click', function () { setViewport(b.dataset.vp); }); });
  Array.prototype.forEach.call(document.querySelectorAll('.mode'), function (b) { b.addEventListener('click', function () { setMode(b.dataset.mode); }); });

  // 画面の外側で開始し、ポインターを捕捉してiframeの上も途切れず囲む。
  var rangeDrag = null, rangeBox = document.createElement('div');
  rangeBox.className = 'rangeSelection'; rangeBox.hidden = true;
  document.body.appendChild(rangeBox);
  function finishRange(cancel) {
    if (!rangeDrag) return;
    var d = rangeDrag; rangeDrag = null; rangeBox.hidden = true;
    try { $('stage').releasePointerCapture(d.id); } catch (_) {}
    if (cancel || Math.hypot(d.ex - d.x, d.ey - d.y) < 4) return;
    var f = frame.getBoundingClientRect(), st = $('stage').getBoundingClientRect();
    var left = Math.max(Math.min(d.x, d.ex), f.left, st.left, 0);
    var top = Math.max(Math.min(d.y, d.ey), f.top, st.top, 0);
    var right = Math.min(Math.max(d.x, d.ex), f.right, st.right, innerWidth);
    var bottom = Math.min(Math.max(d.y, d.ey), f.bottom, st.bottom, innerHeight);
    if (right <= left || bottom <= top || !f.width || !f.height) return;
    var sx = frame.clientWidth / f.width, sy = frame.clientHeight / f.height;
    send({ ss: 'selectRect', additive: d.additive, rect: {
      left: (left - f.left) * sx, top: (top - f.top) * sy,
      right: (right - f.left) * sx, bottom: (bottom - f.top) * sy
    } });
  }
  $('stage').addEventListener('pointerdown', function (e) {
    if (mode !== 'edit' || e.button !== 0 || $('frameWrap').contains(e.target)) return;
    // スクロールバー操作を選択開始と取り違えない。
    var s = $('stage'), r = s.getBoundingClientRect();
    if (e.clientX >= r.left + s.clientWidth || e.clientY >= r.top + s.clientHeight) return;
    finishRange(true); e.preventDefault();
    rangeDrag = { id: e.pointerId, x: e.clientX, y: e.clientY, ex: e.clientX, ey: e.clientY,
      additive: e.shiftKey || e.ctrlKey || e.metaKey };
    try { s.setPointerCapture(e.pointerId); } catch (_) {}
  });
  window.addEventListener('pointermove', function (e) {
    if (!rangeDrag || e.pointerId !== rangeDrag.id) return;
    e.preventDefault(); rangeDrag.ex = e.clientX; rangeDrag.ey = e.clientY;
    var d = rangeDrag;
    rangeBox.style.left = Math.min(d.x, d.ex) + 'px'; rangeBox.style.top = Math.min(d.y, d.ey) + 'px';
    rangeBox.style.width = Math.abs(d.ex - d.x) + 'px'; rangeBox.style.height = Math.abs(d.ey - d.y) + 'px';
    rangeBox.hidden = false;
  });
  window.addEventListener('pointerup', function (e) {
    if (!rangeDrag || e.pointerId !== rangeDrag.id) return;
    rangeDrag.ex = e.clientX; rangeDrag.ey = e.clientY; finishRange(false);
  });
  window.addEventListener('pointercancel', function () { finishRange(true); });
  $('stage').addEventListener('lostpointercapture', function () { finishRange(true); });
  window.addEventListener('blur', function () { finishRange(true); });
  window.addEventListener('keydown', function (e) { if (e.key === 'Escape') finishRange(true); });

  // ------------------------------------------------------------ 状態表示
  function renderStatus() {
    var b = $('statusBadge');
    b.className = 'badge';
    if (state.dirty) { b.classList.add('dirty'); b.textContent = '未保存(' + state.count + '件の変更)'; }
    else if (lastSave && state.count) { b.classList.add('saved'); b.textContent = '保存済み(' + state.count + '件・製品コード未反映)'; }
    else b.textContent = '変更なし';
    $('undoBtn').disabled = !state.canUndo; $('redoBtn').disabled = !state.canRedo;
    $('saveBtn').disabled = !state.dirty;
    $('copyBtn').disabled = !lastSave;
  }
  $('undoBtn').addEventListener('click', function () { send({ ss: 'undo' }); });
  $('redoBtn').addEventListener('click', function () { send({ ss: 'redo' }); });
  window.addEventListener('keydown', function (e) {
    var t = e.target && e.target.localName;
    if (t === 'input' || t === 'textarea' || t === 'select') return;
    var mod = e.ctrlKey || e.metaKey, k = (e.key || '').toLowerCase();
    if (mod && !e.shiftKey && k === 'z') { e.preventDefault(); send({ ss: 'undo' }); }
    else if (mod && (k === 'y' || (e.shiftKey && k === 'z'))) { e.preventDefault(); send({ ss: 'redo' }); }
  });
  window.addEventListener('beforeunload', function (e) { if (state.dirty) { e.preventDefault(); e.returnValue = ''; } });

  // ------------------------------------------------------------ 保存
  function collect() {
    return new Promise(function (resolve) {
      var id = ++collectSeq; pendingCollect[id] = resolve;
      send({ ss: 'collect', id: id });
      setTimeout(function () { if (pendingCollect[id]) { delete pendingCollect[id]; resolve(null); } }, 4000);
    });
  }
  $('saveBtn').addEventListener('click', function () {
    if (!session) return;
    collect().then(function (c) {
      if (!c) { toast('画面から変更を取得できませんでした'); return; }
      var wh = VIEWPORTS[vp];
      var payload = {
        schema: 'app-screen-studio/edit/v1', project: session.project,
        screen: { path: c.screen.path, title: c.screen.title },
        timestamp: new Date().toISOString(),
        viewport: { mode: vp, width: wh[0], height: wh[1] },
        edits: c.edits
      };
      return api('/api/save', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Studio-Token': session.token }, body: JSON.stringify(payload) })
        .then(function (r) {
          if (!r.ok) { toast('保存できませんでした: ' + (r.message || r.code)); return; }
          lastSave = r; send({ ss: 'markSaved', snapshot: JSON.stringify(c.edits) });
          $('savePath').textContent = r.path; $('saveInfo').hidden = false;
          renderStatus();
        });
    });
  });
  $('saveInfoClose').addEventListener('click', function () { $('saveInfo').hidden = true; });
  $('copyBtn').addEventListener('click', function () {
    if (!lastSave) return;
    var text = '画面編集室で保存した見た目の編集内容を、元の画面ソースへ反映してください。\n' +
      '編集JSON: ' + lastSave.path + '\n' +
      '手順: JSONを読み、各edit.targetのedit_id→id→selectorの順で対象要素を特定(tagとtext_hintで照合)し、' +
      'before→afterの差分(text / style / attrs)を元ソースへ適用してください。source情報があればそれを優先します。' +
      '一致しない要素は推測で当てず報告してください。反映後は画面を再表示して確認します。\n' +
      '注意: このJSONは見た目の編集記録で、製品コードには未反映です。';
    var prompt = $('aiPrompt');
    // 入力済みの文章は保持し、同じ保存内容の連打では重複させない。
    if (prompt.value.indexOf(text) === -1) {
      prompt.value += (prompt.value ? '\n\n' : '') + text;
      prompt.dispatchEvent(new Event('input', { bubbles: true }));
    }
    $('saveInfo').hidden = true;
    prompt.focus();
    prompt.setSelectionRange(prompt.value.length, prompt.value.length);
    prompt.scrollIntoView({ block: 'nearest' });
    var done = function () { toast('指示文をコピーし、下の指示欄へ入れました（未送信）'); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text, done); });
    else fallbackCopy(text, done);
  });
  function fallbackCopy(text, done) {
    var previous = document.activeElement;
    var ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select();
    var copied = false;
    try { copied = document.execCommand('copy'); } catch (e) { /* 指示欄への挿入は維持する。 */ }
    document.body.removeChild(ta);
    if (previous && previous.isConnected && document.activeElement === document.body) previous.focus();
    if (copied) done();
    else toast('下の指示欄へ入れました（未送信）。クリップボードへのコピーはできませんでした');
  }

  // ------------------------------------------------------------ パネル
  function rgbToHex(v) {
    var m = /^rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)(?:[,\s/]+([\d.]+))?/.exec(v || '');
    if (!m) return '#000000';
    return '#' + [m[1], m[2], m[3]].map(function (n) { return ('0' + Number(n).toString(16)).slice(-2); }).join('');
  }
  function px(v) { var n = parseFloat(v); return isNaN(n) ? '' : String(Math.round(n * 100) / 100); }
  function firstOf(group) { return GROUPS[group][0]; }

  function renderPanel() {
    var has = !!sel;
    $('panelEmpty').hidden = has; $('panelBody').hidden = !has;
    if (!has) return;
    $('fsAlign').hidden = selCount < 2;
    $('multiHint').hidden = selCount < 2;
    $('multiHint').textContent = selCount + '個を選択中。以下の見た目の変更は全部に反映されます(文字・画像の場所は最後に選んだ要素だけ)。';
    var c = sel.computed;
    $('selTag').textContent = '<' + sel.tag + '>' + (sel.id ? ' #' + sel.id : '');
    $('selHint').textContent = sel.cls ? '.' + sel.cls.split(/\s+/).join('.') : '';
    var ti = $('textInput');
    if (document.activeElement !== ti) ti.value = sel.text;
    ti.disabled = !sel.leaf;
    $('textHint').textContent = sel.leaf ? 'ダブルクリックで画面上から直接も編集できます。' : '子要素を含む(または文字のない)要素です。文字だけの要素を選ぶと編集できます。';
    $('textHint').className = sel.leaf ? 'hint' : 'hint warn';
    $('fsImage').hidden = !sel.isImg;
    $('svgOnly').hidden = !sel.isSvg;
    Array.prototype.forEach.call(document.querySelectorAll('[data-prop]'), function (el) {
      if (document.activeElement === el) return;
      var p = el.dataset.prop, v = c[p];
      if (el.type === 'color') el.value = rgbToHex(v);
      else if (el.type === 'number') el.value = el.dataset.unit === 'px' ? px(v) : (v === 'normal' || v === 'none' ? '' : String(parseFloat(v) || ''));
      else if (el.type === 'range') el.value = v === '' ? '1' : v;
      else if (el.localName === 'select') el.value = Array.prototype.some.call(el.options, function (o) { return o.value === v; }) ? v : '';
      else el.value = v === 'none' ? '' : v;
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-group]'), function (el) {
      if (document.activeElement === el) return;
      var g = el.dataset.group, v = c[firstOf(g)];
      if (el.type === 'color') el.value = rgbToHex(v);
      else if (el.type === 'number') el.value = px(v);
      else el.value = Array.prototype.some.call(el.options || [], function (o) { return o.value === v; }) ? v : '';
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-attr]'), function (el) {
      if (document.activeElement !== el) el.value = el.dataset.attr === 'src' ? sel.src : sel.alt;
    });
    var t = /^(-?[\d.]+)px(?:\s+(-?[\d.]+)px)?$/.exec(c.translate || '');
    if (document.activeElement !== $('shiftX')) $('shiftX').value = t ? t[1] : '0';
    if (document.activeElement !== $('shiftY')) $('shiftY').value = t && t[2] ? t[2] : '0';
    syncGlow(c);
  }

  function valueOf(el) {
    var v = el.value;
    if (el.type === 'number') {
      if (v === '') return '';
        if (el.dataset.unit === 'deg') return v + 'deg';
      return v + (el.dataset.unit === undefined || el.dataset.unit === 'px' || el.dataset.group ? 'px' : '');
    }
    return v;
  }
  function styleFor(el) {
    var v = valueOf(el), out = {};
    if (el.dataset.group) GROUPS[el.dataset.group].forEach(function (k) { out[k] = v; });
    else out[el.dataset.prop] = v;
    return out;
  }
  function pushStyle(style, commit) { if (sel) send({ ss: 'set', style: style, commit: commit }); }

  Array.prototype.forEach.call(document.querySelectorAll('[data-prop],[data-group]'), function (el) {
    el.addEventListener('input', function () { if (el.type === 'color' || el.type === 'range') pushStyle(styleFor(el), false); });
    el.addEventListener('change', function () { pushStyle(styleFor(el), true); });
  });
  Array.prototype.forEach.call(document.querySelectorAll('[data-attr]'), function (el) {
    el.addEventListener('change', function () { var a = {}; a[el.dataset.attr] = el.value; if (sel) send({ ss: 'set', attrs: a, commit: true }); });
  });
  $('textInput').addEventListener('input', function () { if (sel && sel.leaf) send({ ss: 'set', text: this.value, commit: false }); });
  $('textInput').addEventListener('change', function () { if (sel && sel.leaf) send({ ss: 'set', text: this.value, commit: true }); });
  Array.prototype.forEach.call(document.querySelectorAll('[data-clear]'), function (b) {
    b.addEventListener('click', function () { var s = {}; s[b.dataset.clear] = 'transparent'; pushStyle(s, true); });
  });
  $('roundBtn').addEventListener('click', function () { var s = {}; GROUPS.radius.forEach(function (k) { s[k] = '50%'; }); pushStyle(s, true); });
  function shift() { pushStyle({ translate: ($('shiftX').value || 0) + 'px ' + ($('shiftY').value || 0) + 'px' }, true); }
  $('shiftX').addEventListener('change', shift); $('shiftY').addEventListener('change', shift);
  $('selParent').addEventListener('click', function () { send({ ss: 'selectParent' }); });
  $('selReset').addEventListener('click', function () { send({ ss: 'resetEl' }); });

  // ------------------------------------------------------------ 材質と文字の光
  var MATERIALS = [
    { id: 'matte', name: 'マット', base: '#8096ac', type: 'matte' },
    { id: 'glass', name: 'クリアガラス', base: '#8fc9e4', type: 'glass' },
    { id: 'frost', name: 'すりガラス', base: '#bdcddd', type: 'frost' },
    { id: 'metal', name: '鏡面金属', base: '#9eacbb', type: 'metal' },
    { id: 'brushed', name: 'ヘアライン金属', base: '#94a3b5', type: 'brushed' },
    { id: 'gold', name: '金色', base: '#bd902d', type: 'metal' },
    { id: 'wood', name: '木目', base: '#be8954', type: 'wood' },
    { id: 'paper', name: '紙', base: '#e9deca', type: 'paper' },
    { id: 'soft', name: 'やわらかい立体', base: '#ccd6e1', type: 'soft' },
    { id: 'neon', name: 'ネオン', base: '#5acbff', type: 'neon' }
  ];
  var materialCurrent = MATERIALS[0], materialChosen = false, materialCustomColors = false;
  function materialRGB(hex) { return [1, 3, 5].map(function (i) { return parseInt(hex.slice(i, i + 2), 16); }); }
  function materialAlpha(hex, alpha) { return 'rgba(' + materialRGB(hex).join(',') + ',' + Math.max(0, Math.min(1, alpha)).toFixed(3) + ')'; }
  function materialMix(hex, target, amount) {
    var a = materialRGB(hex), b = materialRGB(target);
    return '#' + a.map(function (v, i) { return Math.round(v + (b[i] - v) * amount).toString(16).padStart(2, '0'); }).join('');
  }
  function materialPalette(p) {
    if (!materialCustomColors) return [p.base];
    return Array.from({ length: Number($('materialColorCount').value) }, function (_, i) { return $('materialColor' + (i + 1)).value; });
  }
  function materialPaint(p) {
    var colors = materialPalette(p), color = colors[0], g = Number($('materialGloss').value) / 100;
    var transmission = Number($('materialTransmission').value) / 100, blur = Number($('materialBlur').value);
    var glass = p.type === 'glass' || p.type === 'frost', angle = Number($('materialAngle').value);
    var white = function (a) { return materialAlpha('#ffffff', a); }, black = function (a) { return materialAlpha('#071321', a); };
    var surface = colors.map(function (c) {
      if (glass) return materialAlpha(c, 1 - transmission);
      return p.type === 'neon' ? materialMix(c, '#030b16', .83) : c;
    });
    if (surface.length === 1) surface.push(surface[0]);
    var base = ($('materialGradient').value === 'radial' ? 'radial-gradient(ellipse at 35% 25%,' : 'linear-gradient(' + angle + 'deg,') + surface.map(function (c, i) { return c + ' ' + Math.round(i / (surface.length - 1) * 100) + '%'; }).join(',') + ')';
    var image = [], shadow = [], edge = materialMix(color, '#ffffff', .25), filter = 'none';
    if (glass) {
      // 表面の反射、斜めの映り込み、色付きの透過層を別々に重ねる。
      image.push('radial-gradient(ellipse at 12% 0%,' + white(.22 + g * .36) + ',transparent 62%)');
      image.push('linear-gradient(118deg,' + white(.08 + g * .25) + ' 0%,transparent 24%,transparent 49%,' + white(g * .19) + ' 50%,transparent 53%,transparent 82%,' + white(g * .12) + ' 100%)');
      if (p.type === 'frost') image.push('repeating-linear-gradient(0deg,' + white(.05) + ' 0px,transparent 1px,transparent 3px)');
      edge = white(.35 + g * .5);
      shadow.push('inset 1px 1px 0 ' + white(.28 + g * .6), 'inset -1px -1px 0 ' + materialAlpha(color, .45), 'inset 0 0 18px ' + white(g * .16), '0 10px 28px ' + black(.17));
      filter = 'blur(' + blur + 'px) saturate(155%)';
    } else if (p.type === 'metal' || p.type === 'brushed') {
      // 鏡面は明暗の帯、ヘアラインは細い平行傷を主体にする。
      if (p.type === 'brushed') image.push('repeating-linear-gradient(0deg,' + white(.14) + ' 0px,' + white(.03) + ' .6px,' + black(.12) + ' 1px,transparent 1.6px)');
      image.push('linear-gradient(128deg,' + white(.18 + g * .55) + ' 0%,' + black(.12 + g * .26) + ' 21%,' + white(.2 + g * .62) + ' 43%,' + white(g * .12) + ' 48%,' + black(.1 + g * .28) + ' 67%,' + white(g * .45) + ' 100%)');
      shadow.push('inset 0 1px 0 ' + white(.65), 'inset 0 -2px 2px ' + black(.32), 'inset 1px 0 0 ' + white(.22), '0 5px 12px ' + black(.22));
    } else if (p.type === 'wood') {
      image.push('repeating-radial-gradient(ellipse at -30% -80%,' + black(.13) + ' 0px,transparent 2px,' + white(.09) + ' 4px,transparent 7px,transparent 15px)');
      image.push('repeating-radial-gradient(ellipse at -30% -80%,transparent 0px,' + black(.055) + ' 1px,transparent 2px,transparent 5px)');
      image.push('linear-gradient(120deg,' + white(g * .28) + ',transparent 48%,' + black(.15) + ')');
      shadow.push('inset 0 1px 1px ' + white(.35), 'inset 0 -1px 2px ' + black(.2), '0 4px 10px ' + black(.19));
    } else if (p.type === 'paper') {
      image.push('repeating-linear-gradient(0deg,' + black(.035) + ' 0px,transparent 1px,transparent 3px)');
      image.push('repeating-linear-gradient(92deg,' + white(.055) + ' 0px,transparent 1px,transparent 5px)');
      image.push('radial-gradient(ellipse at 20% 10%,' + white(.2) + ',transparent 80%)');
      shadow.push('0 1px 2px ' + black(.12), '0 5px 12px ' + black(.08));
    } else if (p.type === 'soft') {
      image.push('linear-gradient(145deg,' + white(.2 + g * .2) + ',' + black(.08) + ')');
      shadow.push('inset 0 1px 1px ' + white(.5), '7px 7px 16px ' + black(.22), '-5px -5px 14px ' + white(.7));
    } else if (p.type === 'neon') {
      image.push('radial-gradient(ellipse at 50% 0%,' + materialAlpha(color, .18 + g * .2) + ',transparent 70%)');
      edge = materialMix(color, '#ffffff', .3);
      shadow.push('inset 0 0 12px ' + materialAlpha(color, .24 + g * .2), '0 0 8px ' + materialAlpha(color, .4), '0 0 24px ' + materialAlpha(colors[colors.length - 1], .2 + g * .3));
    } else {
      image.push('radial-gradient(ellipse at 20% 0%,' + white(.1 + g * .09) + ',transparent 75%)');
      image.push('linear-gradient(180deg,transparent,' + black(.07) + ')');
      shadow.push('inset 0 1px 0 ' + white(.22), '0 3px 8px ' + black(.14));
    }
    image.push(base);
    var brightness = colors.reduce(function (sum, c) { var rgb = materialRGB(c); return sum + (rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722); }, 0) / colors.length;
    return { backgroundColor: glass ? 'transparent' : surface[0], backgroundImage: image.join(','),
      boxShadow: ['inset 0 0 0 1px ' + edge].concat(shadow).join(','), backdropFilter: filter,
      edge: edge, ink: p.type === 'neon' || brightness < 140 ? '#f1f7ff' : '#192b3d' };
  }
  function materialSampleStyle(el, paint) {
    ['backgroundColor', 'backgroundImage', 'boxShadow', 'backdropFilter'].forEach(function (k) { el.style[k] = paint[k]; });
    el.style.borderColor = paint.edge; el.style.color = paint.ink;
  }
  function updateMaterialPreviews() {
    var count = Number($('materialColorCount').value);
    for (var i = 1; i <= 4; i++) $('materialColor' + i).parentElement.hidden = i > count;
    $('materialGradientRow').hidden = count < 2;
    $('materialAngleRow').hidden = $('materialAngleValue').hidden = count < 2 || $('materialGradient').value === 'radial';
    $('materialAngleValue').textContent = $('materialAngle').value + '度';
    var glass = materialCurrent.type === 'glass' || materialCurrent.type === 'frost';
    $('materialTransmission').disabled = $('materialBlur').disabled = !glass;
    $('materialValues').textContent = '光沢' + $('materialGloss').value + (glass ? ' / 透明度' + $('materialTransmission').value + ' / ぼかし' + $('materialBlur').value + 'px' : '');
    materialSampleStyle($('materialPreview'), materialPaint(materialCurrent));
    $('materialPreview').textContent = materialCurrent.name + 'の見本';
    MATERIALS.forEach(function (p) {
      var b = document.querySelector('[data-material="' + p.id + '"]');
      materialSampleStyle(b.querySelector('.materialSample'), materialPaint(p));
      b.classList.toggle('on', materialChosen && materialCurrent === p);
      b.setAttribute('aria-pressed', String(materialChosen && materialCurrent === p));
    });
  }
  function applyMaterial(commit) {
    var scope = $('materialScope').value;
    if (scope !== 'screen' && !sel) { if (commit) toast('先にボタンや枠を選んでください'); return; }
    var paint = materialPaint(materialCurrent), style = {};
    ['backgroundColor', 'backgroundImage', 'boxShadow', 'backdropFilter'].forEach(function (k) { style[k] = paint[k]; });
    if ($('materialTextColor').checked) style.color = paint.ink;
    SIDES.forEach(function (side) { style['border' + side + 'Color'] = paint.edge; });
    // 背景全体には外側の影や枠を追加せず、寸法を保つ。
    if (scope === 'screen') { style.boxShadow = 'none'; SIDES.forEach(function (side) { delete style['border' + side + 'Color']; }); }
    send({ ss: 'set', scope: scope, style: style, commit: commit });
    $('materialStatus').textContent = materialCurrent.name + 'を「' + $('materialScope').selectedOptions[0].textContent + '」へ適用';
  }
  MATERIALS.forEach(function (p) {
    var b = document.createElement('button'); b.type = 'button'; b.className = 'materialPreset'; b.dataset.material = p.id;
    var stage = document.createElement('span'); stage.className = 'materialSampleStage';
    var sample = document.createElement('span'); sample.className = 'materialSample'; sample.setAttribute('aria-hidden', 'true'); stage.appendChild(sample);
    b.appendChild(stage); b.appendChild(document.createTextNode(p.name));
    b.addEventListener('click', function () {
      materialCurrent = p; materialChosen = true;
      if (!materialCustomColors) $('materialColor1').value = p.base;
      if (p.type === 'glass' || p.type === 'frost') { $('materialBlur').value = p.type === 'glass' ? '6' : '18'; }
      updateMaterialPreviews(); applyMaterial(true);
    });
    $('materialPresets').appendChild(b);
  });
  ['materialColor1', 'materialColor2', 'materialColor3', 'materialColor4', 'materialColorCount', 'materialGradient', 'materialAngle', 'materialGloss', 'materialTransmission', 'materialBlur'].forEach(function (id) {
    $(id).addEventListener('input', function () {
      if (/^materialColor/.test(id)) materialCustomColors = true;
      updateMaterialPreviews(); if (materialChosen) applyMaterial(false);
    });
    $(id).addEventListener('change', function () {
      if (/^materialColor/.test(id)) materialCustomColors = true;
      updateMaterialPreviews(); if (materialChosen) applyMaterial(true);
    });
  });
  $('materialApply').addEventListener('click', function () { materialChosen = true; updateMaterialPreviews(); applyMaterial(true); });
  $('materialDefaults').addEventListener('click', function () {
    materialCustomColors = false; $('materialColorCount').value = '1'; $('materialGradient').value = 'linear'; $('materialColor1').value = materialCurrent.base;
    updateMaterialPreviews(); if (materialChosen) applyMaterial(true);
  });
  updateMaterialPreviews();

  $('materialColor1').value = materialCurrent.base;
  var glowOn = false;
  function glowShadow() {
    var amount = Number($('glowStrength').value) / 100, color = $('glowColor').value;
    if (!amount) return 'none';
    var r = parseInt(color.slice(1, 3), 16), g = parseInt(color.slice(3, 5), 16), b = parseInt(color.slice(5, 7), 16);
    function layer(blur, alpha) { return '0 0 ' + (blur * amount).toFixed(1) + 'px rgba(' + r + ',' + g + ',' + b + ',' + (alpha * amount).toFixed(2) + ')'; }
    return layer(5, 1) + ',' + layer(20, .9) + ',' + layer(42, .75);
  }
  function previewGlow() {
    $('glowValue').value = $('glowStrength').value + ' / 100';
    $('glowPreview').style.color = $('glowColor').value;
    $('glowPreview').style.textShadow = glowShadow();
  }
  function syncGlow(computed) {
    if (document.activeElement === $('glowStrength') || document.activeElement === $('glowColor')) return;
    var value = computed.textShadow || 'none';
    if (value === 'none') { glowOn = false; $('glowStrength').value = '0'; previewGlow(); return; }
    // 再選択・元に戻す・再適用の後も、保存したCSSから光の色と強さを読み戻す。
    var color = /rgba?\(\s*(\d+),\s*(\d+),\s*(\d+)(?:,\s*[\d.]+)?\)/.exec(value);
    if (!color) return;
    $('glowColor').value = '#' + [color[1], color[2], color[3]].map(function (v) { return Number(v).toString(16).padStart(2, '0'); }).join('');
    var lengths = value.replace(/rgba?\([^)]*\)/g, '').match(/[\d.]+px/g) || [];
    var blur = Math.max.apply(null, lengths.map(parseFloat).concat([0]));
    $('glowStrength').value = String(Math.min(100, Math.max(0, Math.round(blur / 42 * 100))));
    glowOn = true; previewGlow();
  }
  function applyGlow(commit) { previewGlow(); if (sel && glowOn) pushStyle({ textShadow: glowShadow() }, commit); }
  [['青い光', '#55ccff'], ['桃色の光', '#ff75c8'], ['金色の光', '#ffd568'], ['白い光', '#ffffff']].forEach(function (p) {
    var b = document.createElement('button'); b.type = 'button'; b.className = 'btn small glowPreset'; b.textContent = p[0]; b.dataset.glowColor = p[1];
    b.style.color = p[1]; b.style.textShadow = '0 0 5px ' + p[1];
    b.addEventListener('click', function () { glowOn = true; $('glowColor').value = p[1]; if (Number($('glowStrength').value) === 0) $('glowStrength').value = '50'; pushStyle({ color: p[1], textShadow: glowShadow() }, true); previewGlow(); });
    $('glowPresets').appendChild(b);
  });
  ['glowStrength', 'glowColor'].forEach(function (id) {
    $(id).addEventListener('input', function () { glowOn = true; applyGlow(false); });
    $(id).addEventListener('change', function () { glowOn = true; applyGlow(true); });
  });
  $('glowOff').addEventListener('click', function () { glowOn = false; $('glowStrength').value = '0'; previewGlow(); pushStyle({ textShadow: 'none' }, true); });
  previewGlow();

  // ------------------------------------------------------------ 追加・複製削除・整列・画像取込
  Array.prototype.forEach.call(document.querySelectorAll('[data-add]'), function (b) {
    b.addEventListener('click', function () { send({ ss: 'add', kind: b.dataset.add }); });
  });
  Array.prototype.forEach.call(document.querySelectorAll('[data-align]'), function (b) {
    b.addEventListener('click', function () { send({ ss: 'align', how: b.dataset.align }); });
  });
  $('dupBtn').addEventListener('click', function () { send({ ss: 'duplicate' }); });
  $('delBtn').addEventListener('click', function () { send({ ss: 'delete' }); });

  var imageMode = 'add';
  function pickImage(m) { imageMode = m; $('imageFile').value = ''; $('imageFile').click(); }
  $('addImageBtn').addEventListener('click', function () { pickImage('add'); });
  $('replaceImageBtn').addEventListener('click', function () { pickImage('replace'); });
  $('imageFile').addEventListener('change', function () {
    var f = this.files && this.files[0];
    if (!f || !session) return;
    if (f.size > 5 * 1024 * 1024) { toast('画像は5MBまでです'); return; }
    f.arrayBuffer().then(function (buf) {
      return api('/api/asset?name=' + encodeURIComponent(f.name), { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Studio-Token': session.token }, body: buf });
    }).then(function (r) {
      if (!r.ok) { toast('画像を取り込めませんでした: ' + (r.message || r.code)); return; }
      if (imageMode === 'replace') send({ ss: 'set', attrs: { src: r.url }, commit: true });
      else send({ ss: 'add', kind: 'image', src: r.url });
      toast('画像を取り込みました: ' + r.name);
    });
  });

  // ------------------------------------------------------------ AIへの依頼・スクショ・添付
  // 送信は「送信登録」まで。相手の受領・完了は分からないので、画面にもそう出す。
  var ai = { configured: false, missing: [], worktree: '', target: '' }, aiBusy = false, shotBusy = false, atts = [];
  var MAX_ATT = 8;
  var pendingUploads = 0;
  function setAiNote(text, warn) { var n = $('aiNote'); n.textContent = text; n.className = warn ? 'hint warn' : 'hint'; }
  function renderAi() {
    var ok = ai.configured;
    $('aiSendBtn').disabled = !ok || aiBusy || shotBusy || pendingUploads > 0;
    $('shotBtn').disabled = !ok || aiBusy || shotBusy;
    $('attachBtn').disabled = !session || aiBusy;
    $('aiExportBtn').disabled = !session || aiBusy || shotBusy || pendingUploads > 0;
    $('aiSendBtn').textContent = aiBusy ? '送信登録中…' : 'AIへ送信';
  }
  function showAiConfig(a, verified, reasons) {
    ai = a;
    if (a.configured) {
      $('aiState').textContent = '送信先 ' + a.target + ' / workspace ' + a.worktree + (verified === true ? '(紐づけ確認OK)' : verified === false ? '(要確認)' : '');
      setAiNote(reasons && reasons.length ? '送信前に確認が必要: ' + reasons.join(' / ') : '', !!(reasons && reasons.length));
    } else {
      $('aiState').textContent = 'ファイルでAIへ渡すモード';
      setAiNote('「AI用ファイルを保存」で依頼文を作り、使用中のAIへファイルを渡せます。Orcaの直接送信・撮影を使う場合だけ接続設定が必要です。コピーやファイル保存では送信しません。');
    }
    renderAi();
  }
  function refreshAiStatus() {
    api('/api/ai/status', { headers: { 'X-Studio-Token': session.token } }).then(function (r) {
      if (r.ok) showAiConfig(r.ai, r.ai.verified, r.ai.reasons);
    });
  }
  function renderThumbs() {
    var box = $('aiThumbs'); box.textContent = '';
    atts.forEach(function (a, i) {
      var w = document.createElement('div'); w.className = 'thumb';
      var img = document.createElement('img'); img.src = a.url; img.alt = a.label;
      var cap = document.createElement('div'); cap.textContent = a.label;
      var x = document.createElement('button'); x.className = 'thumbX'; x.textContent = '×'; x.title = '添付から外す';
      x.addEventListener('click', function () { atts.splice(i, 1); renderThumbs(); });
      w.appendChild(img); w.appendChild(cap); w.appendChild(x); box.appendChild(w);
    });
  }
  function uploadImage(kind, file) {
    if (!session) { toast('起動が終わってから添付してください'); return Promise.resolve(); }
    if (atts.length + pendingUploads >= MAX_ATT) { toast('添付は' + MAX_ATT + '枚までです'); return Promise.resolve(); }
    if (file.size > 5 * 1024 * 1024) { toast('画像は5MBまでです'); return Promise.resolve(); }
    pendingUploads++; renderAi();
    return file.arrayBuffer().then(function (buf) {
      return api('/api/ai/attach?kind=' + kind + '&name=' + encodeURIComponent(file.name || 'image.png'),
        { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Studio-Token': session.token }, body: buf });
    }).then(function (r) {
      if (!r.ok) { toast('画像を添付できませんでした: ' + (r.message || r.code)); return; }
      atts.push({ name: r.name, url: r.url, label: r.label, kind: r.kind });
      renderThumbs();
    }).catch(function () { toast('画像を取り込めませんでした。もう一度選んでください'); })
      .then(function () { pendingUploads--; renderAi(); });
  }
  $('attachBtn').addEventListener('click', function () { $('aiFile').value = ''; $('aiFile').click(); });
  $('aiFile').addEventListener('change', function () {
    Array.prototype.forEach.call(this.files || [], function (f) { uploadImage('file', f); });
  });
  $('aiPrompt').addEventListener('paste', function (e) {
    var items = (e.clipboardData && e.clipboardData.items) || [], imgs = [], hasText = false;
    Array.prototype.forEach.call(items, function (it) {
      if (it.kind === 'file' && it.type.indexOf('image/') === 0) imgs.push(it.getAsFile());
      if (it.kind === 'string' && it.type === 'text/plain') hasText = true;
    });
    if (imgs.length && !hasText) { e.preventDefault(); imgs.forEach(function (f) { if (f) uploadImage('paste', f); }); }
  });

  // 画面(iframe)の範囲だけを撮る。親のプロンプトや他のタブは写さない(サーバーが切り抜く)
  $('shotBtn').addEventListener('click', function () {
    if (!session || !ai.configured || shotBusy || aiBusy) return;
    if (atts.length >= MAX_ATT) { toast('添付は' + MAX_ATT + '枚までです'); return; }
    shotBusy = true; renderAi();
    document.body.classList.add('capturing'); send({ ss: 'overlay', visible: false });
    frame.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    var wait = new Promise(function (res) {
      // 非表示タブで描画コールバックが止まっても撮影待ちを残さない。
      var fallback = setTimeout(res, 700);
      requestAnimationFrame(function () { requestAnimationFrame(function () { setTimeout(function () { clearTimeout(fallback); res(); }, 150); }); });
    });
    wait.then(function () {
      // 撮る範囲は iframe ∩ stage(スクロール領域) ∩ viewport。親のプロンプト欄や隠れた部分は含めない
      var r = frame.getBoundingClientRect(), st = $('stage').getBoundingClientRect();
      var x1 = Math.max(r.left, st.left, 0), y1 = Math.max(r.top, st.top, 0);
      var x2 = Math.min(r.right, st.right, window.innerWidth), y2 = Math.min(r.bottom, st.bottom, window.innerHeight);
      if (x2 - x1 < 8 || y2 - y1 < 8) { throw new Error('outside'); }
      return api('/api/ai/screenshot', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Studio-Token': session.token },
        body: JSON.stringify({ rect: { x: x1, y: y1, w: x2 - x1, h: y2 - y1 }, viewport: { w: window.innerWidth, h: window.innerHeight },
          full: { w: r.width, h: r.height } }) });
    }).then(function (r) {
      if (!r.ok) { toast('撮影できませんでした: ' + (r.message || r.code)); return; }
      atts.push({ name: r.name, url: r.url, label: r.label, kind: 'shot' }); renderThumbs();
      toast(r.clipped ? '画面の一部だけが写りました(ウィンドウを広げるかスクロールして撮り直せます)' : 'スクショを添付しました');
    }).catch(function (e) { toast(e && e.message === 'outside' ? '画面が見える範囲に入っていません。スクロールして撮り直してください' : '撮影の結果を受け取れませんでした'); }).then(function () {
      document.body.classList.remove('capturing'); send({ ss: 'overlay', visible: true });
      shotBusy = false; renderAi();
    });
  });

  function randomId() {
    var a = new Uint8Array(12); crypto.getRandomValues(a);
    return Array.prototype.map.call(a, function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
  }
  $('aiSendBtn').addEventListener('click', function () {
    if (aiBusy || shotBusy || pendingUploads || !session || !ai.configured) return;      // 連打防止
    var prompt = $('aiPrompt').value;
    if (!prompt.trim()) { toast('依頼文を入力してください'); return; }
    aiBusy = true; renderAi(); setAiNote('送信登録しています。終わるまでお待ちください…');
    var sentAtts = atts.map(function (a) { return a.name; }), edits = null;
    collect().then(function (c) {
      if (!c) throw new Error('collect');
      edits = c.edits;
      var wh = VIEWPORTS[vp];
      var payload = { schema: 'app-screen-studio/edit/v1', project: session.project,
        screen: { path: c.screen.path, title: c.screen.title }, timestamp: new Date().toISOString(),
        viewport: { mode: vp, width: wh[0], height: wh[1] }, edits: c.edits };
      return api('/api/ai/send', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Studio-Token': session.token },
        body: JSON.stringify({ request_id: randomId(), prompt: prompt, attachments: sentAtts, payload: payload }) });
    }).then(function (r) {
      if (r.saved_path && edits) {            // 編集JSONは保存済み(送信の成否とは別)
        lastSave = { path: r.saved_path }; send({ ss: 'markSaved', snapshot: JSON.stringify(edits) });
        $('savePath').textContent = r.saved_path; renderStatus();
      }
      if (r.ok) {
        if ($('aiPrompt').value === prompt) $('aiPrompt').value = '';
        atts = atts.filter(function (a) { return sentAtts.indexOf(a.name) === -1; }); renderThumbs();
        setAiNote('送信登録しました(相手の受領・完了は未確認です)。依頼文: ' + r.request_file);
        toast('送信登録しました(受領は未確認)');
      } else if (r.ambiguous) {
        setAiNote('送信できたか不明です(' + (r.message || r.code) + ')。受信側を確認してから、必要なら自分で「AIへ送信」を押し直してください。自動では再送しません。入力と添付は残してあります。', true);
      } else {
        setAiNote('送信できませんでした: ' + (r.message || r.code) + '。入力と添付は残してあります。', true);
      }
    }).catch(function () {
      setAiNote('送信の結果を受け取れませんでした。送れたか不明です。受信側を確認してから、必要なら自分で押し直してください(自動再送なし)。入力と添付は残してあります。', true);
    }).then(function () { aiBusy = false; renderAi(); });
  });
  $('aiExportBtn').addEventListener('click', function () {
    if (aiBusy || shotBusy || pendingUploads || !session) return;
    var prompt = $('aiPrompt').value;
    if (!prompt.trim()) { toast('依頼文を入力してください'); return; }
    aiBusy = true; renderAi();
    var edits = null;
    collect().then(function (c) {
      if (!c) throw new Error('collect');
      edits = c.edits;
      var wh = VIEWPORTS[vp];
      return api('/api/ai/export', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Studio-Token': session.token },
        body: JSON.stringify({ prompt: prompt, attachments: atts.map(function (a) { return a.name; }),
          payload: { schema: 'app-screen-studio/edit/v1', project: session.project, screen: c.screen,
            timestamp: new Date().toISOString(), viewport: { mode: vp, width: wh[0], height: wh[1] }, edits: edits } }) });
    }).then(function (r) {
      if (!r.ok) { setAiNote('保存できませんでした: ' + (r.message || r.code) + '。入力と添付は残しています。', true); return; }
      lastSave = { path: r.saved_path }; send({ ss: 'markSaved', snapshot: JSON.stringify(edits) });
      $('savePath').textContent = r.saved_path; renderStatus();
      setAiNote('依頼文を保存しました（未送信）。使用中のAIでこのファイルを指定してください: ' + r.request_file);
      toast('AI用ファイルを保存しました（未送信）');
    }).catch(function () { setAiNote('保存結果を確認できませんでした。入力と添付は残しています。', true); })
      .then(function () { aiBusy = false; renderAi(); });
  });
  window.addEventListener('beforeunload', function (e) {
    if ($('aiPrompt').value.trim() || atts.length) { e.preventDefault(); e.returnValue = ''; }
  });

  document.querySelectorAll('[data-layer]').forEach(function (button) {
    button.addEventListener('click', function () { send({ ss: 'layer', how: button.dataset.layer }); });
  });
  init();
})();

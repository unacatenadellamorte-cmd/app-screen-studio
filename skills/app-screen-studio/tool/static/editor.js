/* アプリ画面編集室: 画面(iframe)内で動く編集エンジン。
 * 操作パネルは親ページ(shell)側。ここは要素の選択・ドラッグ・編集・履歴だけを担当する。
 * inline script / inline style 属性 / eval は使わない(CSSOMでのスタイル設定のみ)。 */
(function () {
  'use strict';
  var PREFIX = '/__screen_studio__';

  // 単独で開かれた場合は編集室(shell)へ移す。生の画面を見たい時は ?__studio_raw=1
  if (window.top === window) {
    if (location.search.indexOf('__studio_raw=1') === -1) {
      location.replace(PREFIX + '/shell?path=' + encodeURIComponent(location.pathname + location.search + location.hash));
    }
    return;
  }
  var parentWin = window.parent;
  try {
    if (parentWin.location.origin !== location.origin || parentWin.location.pathname !== PREFIX + '/shell') return;
  } catch (e) { return; }   // 製品が独自に埋め込んだ別iframeでは動かさない

  var SIDES = ['Top', 'Right', 'Bottom', 'Left'];
  var STYLE_KEYS = ['color', 'backgroundColor', 'fontSize', 'fontFamily', 'fontWeight', 'fontStyle',
    'textAlign', 'lineHeight', 'letterSpacing', 'opacity', 'width', 'height', 'translate', 'rotate', 'objectFit',
    'boxShadow', 'backgroundImage', 'backdropFilter', 'textShadow', 'fill', 'stroke', 'strokeWidth', 'position', 'left', 'top', 'zIndex', 'borderTopLeftRadius',
    'borderTopRightRadius', 'borderBottomRightRadius', 'borderBottomLeftRadius'];
  ['padding', 'margin'].forEach(function (p) { SIDES.forEach(function (s) { STYLE_KEYS.push(p + s); }); });
  SIDES.forEach(function (s) { STYLE_KEYS.push('border' + s + 'Width', 'border' + s + 'Color', 'border' + s + 'Style'); });
  var ATTR_KEYS = ['src', 'alt'];
  var SOURCE_ATTRS = ['data-source', 'data-source-file', 'data-source-line', 'data-component', 'data-src-file'];
  var NON_TEXT_TAGS = /^(img|input|textarea|select|svg|script|style|br|hr|canvas|video|audio|iframe|option)$/i;
  var SAFE_IMG_SRC = /^\s*(javascript|vbscript|data|file|blob):/i;

  var mode = 'edit';
  var selection = [];                 // 選択中の要素(最後が主)
  var hoverEl = null;
  var editing = null;                 // {el, before}
  var touched = new Map();            // el -> {text, attrs, inline, computed, target}
  var created = new Map();            // 編集室が作った要素 el -> {kind, shape, cloneOf}
  var deleted = new Map();            // 削除した元要素 el -> target情報
  var newSeq = 0;
  var undoStack = [], redoStack = [], savedPos = 0;
  var session = null;                 // 未確定のライブ変更 {base:[{el,kind,key,from}]}
  var drag = null;
  var host, shadow, boxEl, hoverBox, chip, mkDiv, handles = {}, extraBoxes = [];
  var rafPending = false;

  function primary() { return selection.length ? selection[selection.length - 1] : null; }

  // ------------------------------------------------------------ 通信
  function post(msg) { parentWin.postMessage(msg, location.origin); }
  function toast(text) { post({ ss: 'toast', text: text }); }

  window.addEventListener('message', function (e) {
    if (e.source !== parentWin || e.origin !== location.origin) return;
    var m = e.data;
    if (!m || typeof m !== 'object' || typeof m.ss !== 'string') return;
    switch (m.ss) {
      case 'mode': setMode(m.mode === 'interact' ? 'interact' : 'edit'); break;
      case 'undo': undo(); break;
      case 'redo': redo(); break;
      case 'set': applyFromPanel(m); break;
      case 'selectRect': selectRect(m.rect, m.additive); break;
      case 'selectParent': { var p = primary(); if (p && p.parentElement && p.parentElement !== document.documentElement) select(p.parentElement, false); break; }
      case 'resetEl': resetElement(); break;
      case 'align': alignSelection(m.how); break;
      case 'duplicate': duplicateSelection(); break;
      case 'delete': deleteSelection(); break;
      case 'layer': changeLayer(m.how); break;
      case 'add': addShape(m.kind, m.src); break;
      case 'collect': post({ ss: 'edits', id: m.id, edits: collect(), screen: screenInfo(), viewportWidth: window.innerWidth, revision: undoStack.length }); break;
      case 'reapply': reapply(Array.isArray(m.edits) ? m.edits : []); break;
      case 'markSaved': if (JSON.stringify(collect()) === m.snapshot) savedPos = undoStack.length; sendState(); break;
      case 'ping': sendReady(); break;
      case 'overlay': if (host) host.style.display = m.visible === false ? 'none' : ''; break;
    }
  });

  function screenInfo() {
    var p = location.pathname + location.search + location.hash;
    return { path: p, title: document.title || '' };
  }
  function sendReady() { var s = screenInfo(); post({ ss: 'ready', path: s.path, title: s.title, mode: mode }); }

  // ------------------------------------------------------------ 補助
  function isLeafText(el) {
    if (!el || el.nodeType !== 1 || NON_TEXT_TAGS.test(el.localName) || el instanceof SVGElement) return false;
    return el.children.length === 0 && el.textContent.length > 0;
  }
  function attrEsc(s) { return String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"'); }
  function cssEsc(s) { return (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/[^\w-]/g, '\\$&'); }

  function selectorFor(el) {
    var eid = el.getAttribute('data-edit-id');
    if (eid) return '[data-edit-id="' + attrEsc(eid) + '"]';
    if (el.id && document.querySelectorAll('#' + cssEsc(el.id)).length === 1) return '#' + cssEsc(el.id);
    var parts = [], cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.documentElement) {
      if (cur !== el && cur.id && document.querySelectorAll('#' + cssEsc(cur.id)).length === 1) {
        parts.unshift('#' + cssEsc(cur.id)); break;
      }
      var seg = cur.localName, n = 0, same = 0, sib = cur.parentElement ? cur.parentElement.children : [];
      for (var i = 0; i < sib.length; i++) {
        if (sib[i].localName === cur.localName) { same++; if (sib[i] === cur) n = same; }
      }
      if (same > 1) seg += ':nth-of-type(' + n + ')';
      parts.unshift(seg);
      if (cur === document.body) break;
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  }

  function sourceOf(el) {
    var out = null;
    for (var cur = el; cur && cur.nodeType === 1; cur = cur.parentElement) {
      for (var i = 0; i < SOURCE_ATTRS.length; i++) {
        var v = cur.getAttribute(SOURCE_ATTRS[i]);
        var k = SOURCE_ATTRS[i].slice(5);
        if (v && (!out || !(k in out)) && v.length <= 300) { out = out || {}; out[k] = v; }
      }
      if (out) break;
    }
    return out;
  }

  function signatureFor(el) {
    var text = el.outerHTML, h = 2166136261;
    for (var i = 0; i < text.length; i++) h = Math.imul(h ^ text.charCodeAt(i), 16777619);
    return ('00000000' + (h >>> 0).toString(16)).slice(-8);
  }
  function targetOf(el, textHint) {
    var t = { selector: selectorFor(el), tag: el.localName, signature: signatureFor(el) };
    var eid = el.getAttribute('data-edit-id');
    if (eid) t.edit_id = eid;
    if (el.id) t.id = el.id;
    var hint = (textHint != null ? textHint : (el.textContent || '')).replace(/\s+/g, ' ').trim().slice(0, 40);
    if (hint) t.text_hint = hint;
    return t;
  }

  function parseTranslate(str) {
    var m = /^(-?[\d.]+)px(?:\s+(-?[\d.]+)px)?$/.exec(str || '');
    return m ? { x: parseFloat(m[1]), y: m[2] ? parseFloat(m[2]) : 0 } : { x: 0, y: 0 };
  }
  function translateOf(el) {
    var cs = getComputedStyle(el);
    return parseTranslate(el.style.translate || (cs.translate !== 'none' ? cs.translate : ''));
  }
  function rotateOf(el) {
    var m = /^(-?[\d.]+)deg$/.exec(el.style.rotate || getComputedStyle(el).rotate || '');
    return m ? parseFloat(m[1]) : 0;
  }

  // ------------------------------------------------------------ 変更履歴
  function ensureOrig(el, kind, key) {
    var o = touched.get(el);
    if (!o) {
      o = { text: null, attrs: {}, inline: {}, computed: {}, target: targetOf(el) };
      touched.set(el, o);
    }
    if (kind === 'style' && !(key in o.inline)) {
      o.inline[key] = el.style[key];
      o.computed[key] = getComputedStyle(el)[key];
    } else if (kind === 'text' && o.text === null) {
      o.text = el.textContent;
    } else if (kind === 'attr' && !(key in o.attrs)) {
      o.attrs[key] = el.getAttribute(key);
    }
  }
  function readVal(el, kind, key) {
    if (kind === 'style') return el.style[key];
    if (kind === 'text') return el.textContent;
    return el.getAttribute(key);
  }
  // op: {el, kind: style|text|attr|dom, key, from, to, parent?, next?, info?}
  function applyOp(op, useTo) {
    var val = useTo ? op.to : op.from, el = op.el;
    if (op.kind === 'style') { el.style[op.key] = val == null ? '' : val; }
    else if (op.kind === 'text') { if (el.children.length === 0) el.textContent = val; }
    else if (op.kind === 'attr') { if (val == null) el.removeAttribute(op.key); else el.setAttribute(op.key, val); }
    else if (op.kind === 'dom') {
      if (val === 'in') {
        if (!el.isConnected) op.parent.insertBefore(el, op.next && op.next.parentNode === op.parent ? op.next : null);
        deleted.delete(el);
      } else {
        el.remove();
        if (op.info && !created.has(el)) deleted.set(el, op.info);
      }
    }
  }
  function writeLive(el, kind, key, val) { applyOp({ el: el, kind: kind, key: key, from: val, to: val }, true); }

  function pushGroup(ops) {
    ops = ops.filter(function (o) { return o.from !== o.to; });
    if (!ops.length) return;
    if (savedPos > undoStack.length) savedPos = -1;   // 保存点が未来側にあった
    undoStack.push(ops); redoStack = [];
    afterChange();
  }
  function undo() {
    cancelSession(); if (editing) endTextEdit(false);
    var g = undoStack.pop(); if (!g) return;
    for (var i = g.length - 1; i >= 0; i--) applyOp(g[i], false);
    redoStack.push(g); afterChange();
  }
  function redo() {
    cancelSession(); if (editing) endTextEdit(false);
    var g = redoStack.pop(); if (!g) return;
    for (var i = 0; i < g.length; i++) applyOp(g[i], true);
    undoStack.push(g); afterChange();
  }
  function afterChange() {
    selection = selection.filter(function (e) { return e.isConnected; });
    sendState(); sendSelection(); refresh();
  }

  function isDirty() { return undoStack.length !== savedPos; }
  function sendState() {
    post({ ss: 'state', dirty: isDirty(), canUndo: undoStack.length > 0, canRedo: redoStack.length > 0, count: collect().length });
  }

  // ライブ変更(ドラッグ中・スライダー中)。確定までは履歴に積まない
  function setLive(el, kind, key, val) {
    ensureOrig(el, kind, key);
    if (!session) session = { base: [] };
    if (!session.base.some(function (b) { return b.el === el && b.kind === kind && b.key === key; })) {
      session.base.push({ el: el, kind: kind, key: key, from: readVal(el, kind, key) });
    }
    writeLive(el, kind, key, val);
    refresh();
  }
  function commitSession() {
    if (!session) return;
    var s = session; session = null;
    pushGroup(s.base.map(function (b) {
      return { el: b.el, kind: b.kind, key: b.key, from: b.from, to: readVal(b.el, b.kind, b.key) };
    }));
  }
  function cancelSession() {
    if (!session) return;
    var s = session; session = null;
    s.base.forEach(function (b) { writeLive(b.el, b.kind, b.key, b.from); });
    refresh();
  }

  // ------------------------------------------------------------ パネルからの変更
  function applyFromPanel(m) {
    var els = selection.slice(), p = primary();
    if (m.scope === 'screen') els = [document.body];
    else if (m.scope === 'parent') els = els.map(function (el) { return el.parentElement; }).filter(function (el, i, all) {
      return el && el !== document.documentElement && all.indexOf(el) === i;
    });
    if (m.scope) {
      els = els.filter(function (el) { return !els.some(function (other) { return other !== el && other.contains(el); }); });
      p = els[els.length - 1];
    }
    if (!p) return;
    if (editing) endTextEdit(true);
    var style = m.style && typeof m.style === 'object' ? m.style : {};
    var attrs = m.attrs && typeof m.attrs === 'object' ? m.attrs : {};
    Object.keys(style).forEach(function (k) {
      if (STYLE_KEYS.indexOf(k) !== -1 && typeof style[k] === 'string' && style[k].length <= (/^(backgroundImage|boxShadow|textShadow)$/.test(k) ? 2000 : 300) && !/url\s*\(|expression|javascript/i.test(style[k])) {
        els.forEach(function (el) { setLive(el, 'style', k, style[k]); });
      }
    });
    Object.keys(attrs).forEach(function (k) {
      if (ATTR_KEYS.indexOf(k) === -1 || typeof attrs[k] !== 'string' || attrs[k].length > 2000) return;
      if (k === 'src' && SAFE_IMG_SRC.test(attrs[k])) { toast('画像のURLに使えない形式です'); return; }
      if (p.localName !== 'img') { toast('画像の場所は img 要素だけ変えられます'); return; }
      setLive(p, 'attr', k, attrs[k]);
    });
    if (typeof m.text === 'string') {
      if (!isLeafText(p)) { toast('子要素を含むため、文字は直接置き換えられません。中の要素を選んでください'); return; }
      if (m.text.length > 5000) { toast('文字が長すぎます(5000文字まで)'); return; }
      setLive(p, 'text', 'text', m.text);
    }
    if (m.commit) { commitSession(); } else { sendSelection(); }
  }

  function resetElement() {
    var el = primary(), o = el && touched.get(el);
    if (!o) return;
    cancelSession();
    var ops = [];
    Object.keys(o.inline).forEach(function (k) { ops.push({ el: el, kind: 'style', key: k, from: el.style[k], to: o.inline[k] }); });
    if (o.text !== null && el.children.length === 0) ops.push({ el: el, kind: 'text', key: 'text', from: el.textContent, to: o.text });
    Object.keys(o.attrs).forEach(function (k) { ops.push({ el: el, kind: 'attr', key: k, from: el.getAttribute(k), to: o.attrs[k] }); });
    ops = ops.filter(function (op) { return op.from !== op.to; });
    ops.forEach(function (op) { applyOp(op, true); });
    if (ops.length) { if (savedPos > undoStack.length) savedPos = -1; undoStack.push(ops); redoStack = []; afterChange(); }
  }

  // ------------------------------------------------------------ 追加・複製・削除
  function nextEditId() {
    do { newSeq++; } while (document.querySelector('[data-edit-id="ss-new-' + newSeq + '"]'));
    return 'ss-new-' + newSeq;
  }
  function insertOp(el, parent, next, info) {
    return { el: el, kind: 'dom', key: 'dom', from: 'out', to: 'in', parent: parent, next: next, info: info };
  }
  function commitInsert(el, meta, parent, next) {
    created.set(el, meta);
    var op = insertOp(el, parent, next, null);
    applyOp(op, true);
    pushGroup([op]);
    select(el, false);
  }

  function addShape(kind, src) {
    var el, st = { position: 'absolute' };
    var ox = 60 + (newSeq % 6) * 16, oy = 60 + (newSeq % 6) * 16;
    st.left = Math.round(window.scrollX + ox) + 'px'; st.top = Math.round(window.scrollY + oy) + 'px';
    if (kind === 'rect') {
      el = document.createElement('div');
      Object.assign(st, { width: '140px', height: '90px', backgroundColor: '#3d6b5e', borderTopLeftRadius: '8px', borderTopRightRadius: '8px', borderBottomRightRadius: '8px', borderBottomLeftRadius: '8px' });
    } else if (kind === 'ellipse') {
      el = document.createElement('div');
      Object.assign(st, { width: '100px', height: '100px', backgroundColor: '#d98e04', borderTopLeftRadius: '50%', borderTopRightRadius: '50%', borderBottomRightRadius: '50%', borderBottomLeftRadius: '50%' });
    } else if (kind === 'line') {
      el = document.createElement('div');
      Object.assign(st, { width: '160px', height: '3px', backgroundColor: '#2b2a27' });
    } else if (kind === 'text') {
      el = document.createElement('div');
      el.textContent = 'テキスト';
      Object.assign(st, { fontSize: '20px', color: '#2b2a27' });
    } else if (kind === 'image') {
      if (typeof src !== 'string' || SAFE_IMG_SRC.test(src) || !src) { toast('画像の場所が不正です'); return; }
      el = document.createElement('img');
      el.setAttribute('src', src); el.setAttribute('alt', '');
      Object.assign(st, { width: '160px' });
    } else { return; }
    var id = nextEditId();
    el.setAttribute('data-edit-id', id);
    Object.keys(st).forEach(function (k) { el.style[k] = st[k]; });
    commitInsert(el, { kind: 'add', shape: kind }, document.body, null);
  }

  function structural() {
    var els = selection.filter(function (e) { return e !== document.body && e !== document.documentElement; });
    if (!els.length) { toast('ページ全体(body)は複製・削除できません'); return null; }
    if (editing) endTextEdit(true);
    commitSession();
    return els;
  }
  function duplicateSelection() {
    var els = structural(); if (!els) return;
    var ops = [], clones = [];
    els.forEach(function (el) {
      var c = el.cloneNode(true);
      c.removeAttribute('id'); c.removeAttribute('contenteditable');
      Array.prototype.forEach.call(c.querySelectorAll('[id],[data-edit-id]'), function (d) { d.removeAttribute('id'); d.removeAttribute('data-edit-id'); });
      c.setAttribute('data-edit-id', nextEditId());
      var cloneOf = targetOf(el);
      created.set(c, { kind: 'duplicate', cloneOf: cloneOf });
      var parent = el.parentElement, next = el.nextSibling;
      var op = insertOp(c, parent, next, null);
      applyOp(op, true);
      ops.push(op); clones.push(c);
    });
    pushGroup(ops);
    selection = clones; afterChange();
  }
  function deleteSelection() {
    var els = structural(); if (!els) return;
    var ops = els.map(function (el) {
      var info = touched.has(el) ? touched.get(el).target : targetOf(el);
      var op = { el: el, kind: 'dom', key: 'dom', from: 'in', to: 'out', parent: el.parentElement, next: el.nextSibling, info: info };
      return op;
    });
    ops.forEach(function (op) { applyOp(op, true); });
    selection = [];
    if (savedPos > undoStack.length) savedPos = -1;
    undoStack.push(ops); redoStack = [];
    afterChange();
  }

  // ------------------------------------------------------------ 整列(複数選択)
  function changeLayer(how) {
    var chosen = structural(); if (!chosen) return;
    var parents = [];
    chosen.forEach(function (el) { if (parents.indexOf(el.parentElement) < 0) parents.push(el.parentElement); });
    parents.forEach(function (parent) {
      var nodes = Array.prototype.filter.call(parent.children, function (el) {
        return el !== host && (chosen.indexOf(el) >= 0 || getComputedStyle(el).position !== 'static') &&
          !/^(script|style|link|meta)$/i.test(el.localName);
      });
      nodes.sort(function (a, b) { return (parseInt(getComputedStyle(a).zIndex, 10) || 0) - (parseInt(getComputedStyle(b).zIndex, 10) || 0); });
      var selected = nodes.filter(function (el) { return chosen.indexOf(el) >= 0; });
      if (how === 'front') nodes = nodes.filter(function (el) { return selected.indexOf(el) < 0; }).concat(selected);
      else if (how === 'back') nodes = selected.concat(nodes.filter(function (el) { return selected.indexOf(el) < 0; }));
      else {
        var work = how === 'forward' ? selected.slice().reverse() : selected;
        work.forEach(function (el) {
          var i = nodes.indexOf(el), j = i + (how === 'forward' ? 1 : -1);
          if (j >= 0 && j < nodes.length && selected.indexOf(nodes[j]) < 0) { var other = nodes[j]; nodes[j] = el; nodes[i] = other; }
        });
      }
      nodes.forEach(function (el, i) {
        if (chosen.indexOf(el) >= 0 && getComputedStyle(el).position === 'static') setLive(el, 'style', 'position', 'relative');
        setLive(el, 'style', 'zIndex', String(i + 1));
      });
    });
    commitSession();
  }
  function alignSelection(how) {
    commitSession();
    var els = selection.filter(function (e) { return e.isConnected; });
    if (els.length < 2) { toast('2つ以上の要素を選ぶと整列できます(Shift+クリックで追加)'); return; }
    var items = els.map(function (el) { var r = el.getBoundingClientRect(); return { el: el, r: r, t: translateOf(el), dx: 0, dy: 0 }; });
    var L = Math.min.apply(null, items.map(function (i) { return i.r.left; }));
    var R = Math.max.apply(null, items.map(function (i) { return i.r.right; }));
    var T = Math.min.apply(null, items.map(function (i) { return i.r.top; }));
    var B = Math.max.apply(null, items.map(function (i) { return i.r.bottom; }));
    if (how === 'left') items.forEach(function (i) { i.dx = L - i.r.left; });
    else if (how === 'right') items.forEach(function (i) { i.dx = R - i.r.right; });
    else if (how === 'center') items.forEach(function (i) { i.dx = (L + R) / 2 - (i.r.left + i.r.right) / 2; });
    else if (how === 'top') items.forEach(function (i) { i.dy = T - i.r.top; });
    else if (how === 'bottom') items.forEach(function (i) { i.dy = B - i.r.bottom; });
    else if (how === 'middle') items.forEach(function (i) { i.dy = (T + B) / 2 - (i.r.top + i.r.bottom) / 2; });
    else if (how === 'distH' || how === 'distV') {
      if (items.length < 3) { toast('均等に並べるには3つ以上選んでください'); return; }
      var h = how === 'distH', lo = h ? 'left' : 'top', hi = h ? 'right' : 'bottom';
      var sorted = items.slice().sort(function (a, b) { return a.r[lo] - b.r[lo]; });
      var span = (h ? R - L : B - T), sum = sorted.reduce(function (s, i) { return s + (i.r[hi] - i.r[lo]); }, 0);
      var gap = (span - sum) / (sorted.length - 1), pos = h ? L : T;
      sorted.forEach(function (i) { var d = pos - i.r[lo]; if (h) i.dx = d; else i.dy = d; pos += (i.r[hi] - i.r[lo]) + gap; });
    } else return;
    items.forEach(function (i) {
      if (Math.abs(i.dx) < 0.5 && Math.abs(i.dy) < 0.5) return;
      setLive(i.el, 'style', 'translate', Math.round(i.t.x + i.dx) + 'px ' + Math.round(i.t.y + i.dy) + 'px');
    });
    commitSession();
  }

  // ------------------------------------------------------------ 保存用の差分
  function sideOf(el) {
    var st = {}, at = {};
    STYLE_KEYS.forEach(function (k) { if (el.style[k]) st[k] = el.style[k]; });
    ATTR_KEYS.forEach(function (k) { var v = el.getAttribute(k); if (v) at[k] = v; });
    var s = { style: st, attrs: at };
    if (isLeafText(el)) s.text = el.textContent;
    return s;
  }
  function collect() {
    var adds = [], mods = [], dels = [];
    created.forEach(function (meta, el) {
      if (!el.isConnected || !el.parentElement) return;
      var e = {
        op: meta.kind, target: targetOf(el), before: { style: {}, attrs: {} }, after: sideOf(el),
        parent: targetOf(el.parentElement, ''), index: Array.prototype.indexOf.call(el.parentElement.children, el),
        viewport_width: window.innerWidth
      };
      if (meta.shape) e.shape = meta.shape;
      if (meta.cloneOf) e.clone_of = meta.cloneOf;
      adds.push(e);
    });
    touched.forEach(function (o, el) {
      if (!el.isConnected || created.has(el)) return;
      var before = { style: {}, attrs: {} }, after = { style: {}, attrs: {} }, changed = false;
      Object.keys(o.inline).forEach(function (k) {
        var cur = el.style[k];
        if (cur === o.inline[k]) return;
        before.style[k] = o.computed[k];
        after.style[k] = cur !== '' ? cur : getComputedStyle(el)[k];
        changed = true;
      });
      if (o.text !== null && el.textContent !== o.text) { before.text = o.text; after.text = el.textContent; changed = true; }
      Object.keys(o.attrs).forEach(function (k) {
        var cur = el.getAttribute(k);
        if (cur === o.attrs[k]) return;
        before.attrs[k] = o.attrs[k] == null ? '' : o.attrs[k];
        after.attrs[k] = cur == null ? '' : cur;
        changed = true;
      });
      if (!changed) return;
      var t = Object.assign({}, o.target);
      if (o.text !== null) { var hint = o.text.replace(/\s+/g, ' ').trim().slice(0, 40); if (hint) t.text_hint = hint; }
      var e = { op: 'modify', target: t, before: before, after: after, viewport_width: window.innerWidth };
      var src = sourceOf(el);
      if (src) e.source = src;
      mods.push(e);
    });
    deleted.forEach(function (info) {
      dels.push({ op: 'delete', target: info, before: { style: {}, attrs: {} }, after: { style: {}, attrs: {} }, viewport_width: window.innerWidth });
    });
    return adds.concat(mods, dels);
  }

  // 保存済みの編集を再適用。元の要素が変わった場合は無言で別要素へ当てない。
  function findTarget(t) {
    var found = [];
    try {
      if (t.edit_id) found = document.querySelectorAll('[data-edit-id="' + attrEsc(t.edit_id) + '"]');
      else if (t.id) { var byId = document.getElementById(t.id); found = byId ? [byId] : []; }
      else if (t.selector) found = document.querySelectorAll(t.selector);
    } catch (e) { return { err: 'セレクターが不正' }; }
    if (found.length === 0) return { err: '要素が見つからない' };
    if (found.length > 1) return { err: '候補が複数あり特定できない' };
    if (found[0].localName !== t.tag) return { err: 'タグが違う(' + found[0].localName + ')' };
    return { el: found[0] };
  }
  function applyRestoredSide(el, side) {
    if (typeof side.text === 'string') {
      if (el.children.length) throw new Error('子要素のある文字は置換しません');
      el.textContent = side.text;
    }
    Object.keys(side.style || {}).forEach(function (k) {
      var v = side.style[k];
      if (STYLE_KEYS.indexOf(k) < 0 || typeof v !== 'string' || /url\s*\(|expression|javascript/i.test(v)) throw new Error('未対応のスタイル');
      el.style[k] = v;
    });
    Object.keys(side.attrs || {}).forEach(function (k) {
      var v = side.attrs[k];
      if (ATTR_KEYS.indexOf(k) < 0 || (k === 'src' && SAFE_IMG_SRC.test(v))) throw new Error('未対応の属性');
      el.setAttribute(k, v);
    });
  }
  function restoreStructure(e) {
    var existing = findTarget(e.target), el, parent, meta;
    if (e.op === 'delete') {
      if (existing.err) return existing.err === '要素が見つからない' ? { already: true } : { reason: existing.err };
      el = existing.el;
      if (el === document.body || el === document.documentElement) return { reason: 'ページ全体は削除できません' };
      if (!e.target.signature || signatureFor(el) !== e.target.signature) return { reason: '削除前の要素が変わっています' };
      var del = { el: el, kind: 'dom', key: 'dom', from: 'in', to: 'out', parent: el.parentElement, next: el.nextSibling, info: e.target };
      applyOp(del, true); pushGroup([del]); return { applied: true };
    }
    if (!existing.err) {
      if (e.target.signature && signatureFor(existing.el) !== e.target.signature) return { reason: '同じ識別子の要素の内容が違います' };
      return { already: true };
    }
    if (existing.err !== '要素が見つからない') return { reason: existing.err };
    if (!e.target.edit_id || !/^ss-new-\d+$/.test(e.target.edit_id)) return { reason: '追加要素の識別子がありません' };
    var p = findTarget(e.parent);
    if (p.err) return { reason: '追加先: ' + p.err };
    parent = p.el;
    if (/^(html|input|textarea|select|script|style)$/i.test(parent.localName)) return { reason: '追加先の種類が違います' };
    if (e.op === 'duplicate') {
      var source = findTarget(e.clone_of);
      if (source.err) return { reason: '複製元: ' + source.err };
      if (!created.has(source.el) && e.clone_of.signature && signatureFor(source.el) !== e.clone_of.signature) return { reason: '複製元の内容が変わっています' };
      el = source.el.cloneNode(true);
      el.removeAttribute('id'); el.removeAttribute('contenteditable');
      Array.prototype.forEach.call(el.querySelectorAll('[id],[data-edit-id]'), function (child) { child.removeAttribute('id'); child.removeAttribute('data-edit-id'); });
      meta = { kind: 'duplicate', cloneOf: e.clone_of };
    } else {
      if (['rect', 'ellipse', 'line', 'text', 'image'].indexOf(e.shape) < 0) return { reason: '図形の種類が違います' };
      el = document.createElement(e.shape === 'image' ? 'img' : 'div');
      meta = { kind: 'add', shape: e.shape };
    }
    if (el.localName !== e.target.tag) return { reason: '追加するタグが違います' };
    el.setAttribute('data-edit-id', e.target.edit_id);
    try { applyRestoredSide(el, e.after); } catch (err) { return { reason: err.message }; }
    created.set(el, meta);
    var op = insertOp(el, parent, parent.children[e.index] || null, null);
    applyOp(op, true); pushGroup([op]); return { applied: true };
  }
  function reapply(edits) {
    var applied = 0, already = 0, skipped = [];
    cancelSession();
    edits.forEach(function (e) {
      var t = e && e.target, b = e && e.before, a = e && e.after;
      var label = (t && (t.edit_id || t.id || t.selector)) || '?';
      if (!t || !b || !a) { skipped.push({ target: label, reason: '形式が不正' }); return; }
      if (e.op && e.op !== 'modify') {
        var restored = restoreStructure(e);
        if (restored.reason) skipped.push({ target: label, reason: restored.reason });
        else if (restored.already) already++; else applied++;
        return;
      }
      var r = findTarget(t);
      if (r.err) { skipped.push({ target: label, reason: r.err }); return; }
      var el = r.el, cs = getComputedStyle(el), ops = [], bad = null, same = 0, total = 0;
      Object.keys(a.style || {}).forEach(function (k) {
        if (bad || STYLE_KEYS.indexOf(k) === -1) { bad = bad || '未対応のスタイル: ' + k; return; }
        total++;
        if (el.style[k] === a.style[k]) { same++; return; }
        if (cs[k] !== (b.style || {})[k]) { bad = '元の値が違う: ' + k; return; }
        ops.push({ el: el, kind: 'style', key: k, from: el.style[k], to: a.style[k] });
      });
      if (!bad && typeof a.text === 'string') {
        total++;
        if (el.textContent === a.text) same++;
        else if (el.textContent !== b.text || !isLeafText(el)) bad = '元の文字が違う';
        else ops.push({ el: el, kind: 'text', key: 'text', from: el.textContent, to: a.text });
      }
      Object.keys(a.attrs || {}).forEach(function (k) {
        if (bad || ATTR_KEYS.indexOf(k) === -1) { bad = bad || '未対応の属性: ' + k; return; }
        total++;
        var cur = el.getAttribute(k) || '';
        if (cur === a.attrs[k]) { same++; return; }
        if (cur !== (b.attrs || {})[k]) { bad = '元の属性が違う: ' + k; return; }
        ops.push({ el: el, kind: 'attr', key: k, from: el.getAttribute(k), to: a.attrs[k] });
      });
      if (bad) { skipped.push({ target: label, reason: bad }); return; }
      if (total && same === total) { already++; return; }
      ops.forEach(function (op) { ensureOrig(op.el, op.kind, op.key); });
      ops.forEach(function (op) { applyOp(op, true); });
      pushGroup(ops); applied++;
    });
    post({ ss: 'reapplied', applied: applied, already: already, skipped: skipped });
  }

  // ------------------------------------------------------------ 選択情報
  function describe(el) {
    var cs = getComputedStyle(el), computed = {};
    STYLE_KEYS.forEach(function (k) { computed[k] = cs[k]; });
    var r = el.getBoundingClientRect();
    computed.width = r.width + 'px'; computed.height = r.height + 'px';
    return {
      tag: el.localName, selector: selectorFor(el), id: el.id || '', editId: el.getAttribute('data-edit-id') || '',
      cls: (typeof el.className === 'string' ? el.className : '').trim().slice(0, 80),
      leaf: isLeafText(el), text: isLeafText(el) ? el.textContent : '',
      isImg: el.localName === 'img', isSvg: el instanceof SVGElement,
      src: el.getAttribute('src') || '', alt: el.getAttribute('alt') || '',
      added: created.has(el), rotation: rotateOf(el),
      computed: computed
    };
  }
  function sendSelection() {
    var p = primary();
    post({ ss: 'selection', count: selection.length, info: p && p.isConnected ? describe(p) : null });
  }
  function select(el, additive) {
    commitSession();
    if (editing && !(selection.length === 1 && editing.el === el)) endTextEdit(true);
    if (additive) {
      var i = selection.indexOf(el);
      if (i !== -1) selection.splice(i, 1); else selection.push(el);
    } else selection = el ? [el] : [];
    sendSelection(); refresh();
  }

  // ------------------------------------------------------------ 文字の直接編集
  function startTextEdit(el) {
    if (!isLeafText(el)) { toast('子要素を含む要素の文字は直接置き換えられません。中の文字だけの要素を選んでください'); return; }
    if (editing) endTextEdit(true);
    ensureOrig(el, 'text', 'text');
    editing = { el: el, before: el.textContent };
    el.setAttribute('contenteditable', 'plaintext-only');
    if (el.contentEditable !== 'plaintext-only') el.setAttribute('contenteditable', 'true');
    el.focus();
    var sel = window.getSelection(), range = document.createRange();
    range.selectNodeContents(el); sel.removeAllRanges(); sel.addRange(range);
    refresh();
  }
  function endTextEdit(commit) {
    if (!editing) return;
    var el = editing.el, before = editing.before;
    editing = null;
    el.removeAttribute('contenteditable');
    var now = el.textContent;
    if (!commit || el.children.length > 0) {
      el.textContent = before;      // 子要素が混入した/中止 → 元の文字へ
      if (commit) toast('子要素が混ざるため、文字の変更を取り消しました');
    } else if (now !== before) {
      pushGroup([{ el: el, kind: 'text', key: 'text', from: before, to: now }]);
    }
    window.getSelection().removeAllRanges();
    sendSelection(); refresh();
  }

  // ------------------------------------------------------------ 画面上の操作
  function pickTarget(t) {
    if (t && t.nodeType === 3) t = t.parentElement;
    if (!t || t === document.documentElement || t === host) return null;
    return t;
  }
  // 背景用の入れ物を除き、囲みきった操作部品・文字・画像だけを選ぶ。
  function selectRect(r, additive) {
    if (mode !== 'edit' || !r || !['left', 'top', 'right', 'bottom'].every(function (k) { return Number.isFinite(r[k]); })) return;
    if (r.right <= r.left || r.bottom <= r.top) return;
    clearHold(); cancelSession(); drag = null;
    if (editing) endTextEdit(true);
    var atomic = 'button,[role="button"],a,input,textarea,select,img,svg';
    var candidates = Array.prototype.filter.call(document.body.querySelectorAll('*'), function (el) {
      if (el === host || (host && host.contains(el)) || /^(script|style|link|meta|noscript|option)$/i.test(el.localName)) return false;
      if (el.parentElement && el.parentElement.closest('svg')) return false;
      var cs = getComputedStyle(el), b = el.getBoundingClientRect();
      if (cs.visibility !== 'visible' || cs.display === 'none' || Number(cs.opacity) === 0 || !b.width || !b.height) return false;
      if (b.left < r.left - 0.5 || b.top < r.top - 0.5 || b.right > r.right + 0.5 || b.bottom > r.bottom + 0.5) return false;
      if (el.matches(atomic) || created.has(el)) return true;
      var text = Array.prototype.some.call(el.childNodes, function (n) { return n.nodeType === 3 && n.textContent.trim(); });
      if (Array.prototype.some.call(el.children, function (child) {
        var display = getComputedStyle(child).display;
        return display !== 'none' && display !== 'inline' && display !== 'contents';
      })) return false;
      return text || (/^(p|h[1-6]|label|li)$/i.test(el.localName) && el.textContent.trim());
    });
    // ボタン内の文字やアイコンは親と同時に選ばず、移動量の二重適用を防ぐ。
    var found = candidates.filter(function (el) {
      if (el.parentElement && el.parentElement.closest(atomic)) return false;
      return !candidates.some(function (p) { return p !== el && p.contains(el); });
    });
    var combined = additive ? selection.concat(found) : found;
    selection = combined.filter(function (el, i) {
      return combined.indexOf(el) === i && !combined.some(function (p) { return p !== el && p.contains(el); });
    });
    hoverEl = null; sendSelection(); refresh();
    toast(selection.length ? selection.length + '個を選択。選んだ一つを長押ししてまとめて移動' : '範囲内に要素がありません');
  }
  function blockedInline(el) {
    if (el instanceof SVGElement || el.localName === 'img') return false;
    return getComputedStyle(el).display === 'inline';
  }

  function onPointerDown(e) {
    if (mode !== 'edit' || e.button !== 0) return;
    var path = e.composedPath();
    if (path.indexOf(host) !== -1) {
      var h = path[0];
      if (h && h.dataset && h.dataset.h) startHandle(e, h.dataset.h);
      return;
    }
    if (editing && editing.el.contains(e.target)) return;   // 編集中の文字内はそのまま
    e.preventDefault(); e.stopPropagation();
    var t = pickTarget(e.target);
    if (t) t = selection.find(function (el) { return el.contains(t); }) || t.closest('button, [role=button], a') || t;
    if (!t) return;
    if (editing) endTextEdit(true);
    if (e.shiftKey || e.ctrlKey || e.metaKey) { select(t, true); return; }
    if (selection.indexOf(t) === -1) select(t, false);
    else { selection.splice(selection.indexOf(t), 1); selection.push(t); sendSelection(); refresh(); }
    startMove(e);
  }
  function clearHold() {
    if (drag && drag.holdTimer) clearTimeout(drag.holdTimer);
    document.documentElement.removeAttribute('data-ss-drag-ready');
  }
  function startMove(e) {
    clearHold();
    drag = { type: 'move', x: e.clientX, y: e.clientY, moved: false, ready: false,
      items: selection.filter(function (el) { return !selection.some(function (p) { return p !== el && p.contains(el); }); }).map(function (el) {
        var b = translateOf(el), cs = getComputedStyle(el);
        return { el: el, bx: b.x, by: b.y, inline: blockedInline(el), position: cs.position,
          left: cs.position === 'static' || cs.left === 'auto' ? '0px' : cs.left,
          top: cs.position === 'static' || cs.top === 'auto' ? '0px' : cs.top };
      }) };
    var pending = drag;
    drag.holdTimer = setTimeout(function () {
      if (drag !== pending) return;
      drag.ready = true;
      document.documentElement.setAttribute('data-ss-drag-ready', '');
      toast('そのまま動かすと移動できます');
    }, 450);
  }
  function startHandle(e, dir) {
    var el = primary();
    if (!el || selection.length !== 1) return;
    e.preventDefault(); e.stopPropagation();
    if (dir === 'r') {
      var r0 = el.getBoundingClientRect();
      var cx = r0.left + r0.width / 2, cy = r0.top + r0.height / 2;
      drag = { type: 'rotate', el: el, cx: cx, cy: cy, a0: Math.atan2(e.clientY - cy, e.clientX - cx), base: rotateOf(el), moved: true };
      return;
    }
    if (blockedInline(el)) { toast('文字の流れの中にある要素(inline)はサイズ変更できません。親の要素を選んでください'); return; }
    var cs = getComputedStyle(el), r = el.getBoundingClientRect();
    var extra = cs.boxSizing === 'border-box' ? { w: 0, h: 0 } : {
      w: parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight) + parseFloat(cs.borderLeftWidth) + parseFloat(cs.borderRightWidth),
      h: parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom) + parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth)
    };
    drag = { type: 'resize', dir: dir, el: el, x: e.clientX, y: e.clientY, w: r.width, h: r.height, extra: extra, moved: true };
  }
  function onPointerMove(e) {
    if (mode !== 'edit') return;
    if (!drag) {
      if (e.composedPath().indexOf(host) !== -1 || editing) return;
      var t = pickTarget(e.target);
      if (t !== hoverEl) { hoverEl = t; refresh(); }
      return;
    }
    var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (drag.type === 'move') {
      if (!drag.ready) {
        if (Math.hypot(dx, dy) > 8) { clearHold(); drag = null; }
        return;
      }
      e.preventDefault();
      if (!drag.moved) {
        if (Math.hypot(dx, dy) < 4) return;
        drag.moved = true;
      }
      drag.items.forEach(function (i) {
        if (i.inline) {
          // 行内文字は通常の文字配置を保ったまま、相対位置で移動する。
          if (i.position === 'static') setLive(i.el, 'style', 'position', 'relative');
          setLive(i.el, 'style', 'left', 'calc(' + i.left + ' + ' + Math.round(dx) + 'px)');
          setLive(i.el, 'style', 'top', 'calc(' + i.top + ' + ' + Math.round(dy) + 'px)');
        } else setLive(i.el, 'style', 'translate', Math.round(i.bx + dx) + 'px ' + Math.round(i.by + dy) + 'px');
      });
    } else if (drag.type === 'rotate') {
      var ang = Math.atan2(e.clientY - drag.cy, e.clientX - drag.cx) - drag.a0;
      var deg = drag.base + ang * 180 / Math.PI;
      deg = e.shiftKey ? Math.round(deg / 15) * 15 : Math.round(deg * 10) / 10;
      setLive(drag.el, 'style', 'rotate', deg + 'deg');
    } else {
      if (drag.dir.indexOf('e') !== -1) setLive(drag.el, 'style', 'width', Math.max(4, Math.round(drag.w + dx - drag.extra.w)) + 'px');
      if (drag.dir.indexOf('s') !== -1) setLive(drag.el, 'style', 'height', Math.max(4, Math.round(drag.h + dy - drag.extra.h)) + 'px');
    }
    sendSelection();
  }
  function onPointerUp() {
    if (!drag) return;
    clearHold();
    var d = drag; drag = null;
    if (d.moved) commitSession();
  }

  // 編集モード中は製品側の click / submit 等を通さない
  function swallow(e) {
    if (mode !== 'edit') return;
    if (e.composedPath().indexOf(host) !== -1) return;
    if (editing && editing.el.contains(e.target)) return;
    e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
  }
  function onDblClick(e) {
    if (mode !== 'edit') return;
    if (e.composedPath().indexOf(host) !== -1) return;
    if (editing && editing.el.contains(e.target)) return;
    e.preventDefault(); e.stopPropagation();
    var t = pickTarget(e.target);
    if (t) { select(t, false); startTextEdit(t); }
  }
  function onKeyDown(e) {
    if (mode !== 'edit') return;
    var mod = e.ctrlKey || e.metaKey, k = (e.key || '').toLowerCase();
    if (editing) {
      if (e.key === 'Escape') { e.preventDefault(); endTextEdit(false); }
      else if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); endTextEdit(true); }
      return;     // 文字入力中の Ctrl+Z はブラウザー標準
    }
    if (drag && e.key === 'Escape') { clearHold(); cancelSession(); drag = null; sendSelection(); return; }
    var inField = /^(input|textarea|select)$/i.test(e.target.localName || '');
    if (mod && !e.shiftKey && k === 'z') { e.preventDefault(); e.stopPropagation(); undo(); }
    else if (mod && (k === 'y' || (e.shiftKey && k === 'z'))) { e.preventDefault(); e.stopPropagation(); redo(); }
    else if (mod && k === 'd' && selection.length) { e.preventDefault(); duplicateSelection(); }
    else if ((e.key === 'Delete' || e.key === 'Backspace') && selection.length && !inField) { e.preventDefault(); deleteSelection(); }
    else if (e.key === 'Escape' && selection.length) { select(null, false); }
    else if (e.key === 'Enter' || e.key === ' ') { if (e.target !== document.body) { e.preventDefault(); e.stopPropagation(); } }
  }

  function setMode(m) {
    mode = m;
    if (m !== 'edit') { clearHold(); cancelSession(); drag = null; if (editing) endTextEdit(true); hoverEl = null; }
    document.documentElement.setAttribute('data-ss-mode', m);
    refresh();
  }

  // ------------------------------------------------------------ 選択枠(専用root)
  function buildOverlay() {
    host = document.createElement('div');
    host.setAttribute('data-screen-studio-root', '');
    shadow = host.attachShadow({ mode: 'open' });
    var link = document.createElement('link');
    link.rel = 'stylesheet'; link.href = PREFIX + '/editor.css';
    shadow.appendChild(link);
    var div = mkDiv = function (cls, parent) { var d = document.createElement('div'); d.className = cls; (parent || shadow).appendChild(d); return d; };
    hoverBox = div('hover'); boxEl = div('box'); chip = div('chip', boxEl);
    ['e', 's', 'se', 'r'].forEach(function (d) { var h = div('handle h-' + d, boxEl); h.dataset.h = d; handles[d] = h; });
    div('stem', boxEl);
    document.documentElement.appendChild(host);
  }
  function place(node, r) {
    node.style.left = r.left + 'px'; node.style.top = r.top + 'px';
    node.style.width = r.width + 'px'; node.style.height = r.height + 'px';
  }
  function refresh() {
    if (rafPending) return;
    rafPending = true;
    var finished = false;
    function draw() {
      if (finished) return;
      finished = true; rafPending = false;
      var show = mode === 'edit', p = primary();
      selection = selection.filter(function (e) { return e.isConnected; });
      hoverBox.style.display = show && hoverEl && selection.indexOf(hoverEl) === -1 && hoverEl.isConnected ? 'block' : 'none';
      if (hoverBox.style.display === 'block') place(hoverBox, hoverEl.getBoundingClientRect());
      p = primary();
      boxEl.style.display = show && p ? 'block' : 'none';
      boxEl.classList.toggle('multi', selection.length > 1);
      if (show && p) {
        place(boxEl, p.getBoundingClientRect());
        chip.textContent = selection.length > 1 ? selection.length + '個を選択中' : p.localName + (p.id ? '#' + p.id : '');
      }
      var others = show ? selection.slice(0, -1) : [];
      while (extraBoxes.length < others.length) { var d = mkDiv('box extra'); extraBoxes.push(d); }
      extraBoxes.forEach(function (d, i) {
        if (i < others.length) { d.style.display = 'block'; place(d, others[i].getBoundingClientRect()); } else d.style.display = 'none';
      });
    }
    requestAnimationFrame(draw);
    setTimeout(draw, 100);
  }

  // ------------------------------------------------------------ 初期化
  function init() {
    buildOverlay();
    document.documentElement.setAttribute('data-ss-mode', mode);
    window.addEventListener('pointerdown', onPointerDown, true);
    window.addEventListener('pointermove', onPointerMove, true);
    window.addEventListener('pointerup', onPointerUp, true);
    window.addEventListener('pointercancel', function () { clearHold(); cancelSession(); drag = null; sendSelection(); }, true);
    window.addEventListener('blur', onPointerUp);
    ['mousedown', 'mouseup', 'click', 'auxclick', 'contextmenu', 'submit', 'dragstart', 'touchstart', 'touchend', 'selectstart']
      .forEach(function (t) { window.addEventListener(t, swallow, true); });
    window.addEventListener('dblclick', onDblClick, true);
    window.addEventListener('keydown', onKeyDown, true);
    window.addEventListener('scroll', refresh, true);
    window.addEventListener('resize', refresh);
    window.addEventListener('hashchange', sendReady);
    window.addEventListener('popstate', sendReady);
    ['pushState', 'replaceState'].forEach(function (name) {
      var original = history[name];
      history[name] = function () {
        var previous = location.href;
        var result = original.apply(this, arguments);
        if (location.href !== previous) sendReady();
        return result;
      };
    });
    document.addEventListener('pointerleave', function () { hoverEl = null; refresh(); });
    window.addEventListener('beforeunload', function (e) {
      if (isDirty()) { e.preventDefault(); e.returnValue = ''; }
    });
    sendReady(); sendState();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();

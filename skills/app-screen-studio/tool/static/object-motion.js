/* オブジェクトの動きを再生する共通部品。保存するのは設定だけで、製品側でも同じファイルを使える。 */
(function () {
  'use strict';
  var ATTR = 'data-studio-motion', active = new Map(), bindings = new Map();
  var catalog = [
    ['slide', 'スライド', '画面遷移', false, '選択した要素が外側から入る'],
    ['push', 'プッシュ', '画面遷移', true, '相手を押し出しながら入る'],
    ['pop', 'ポップ（戻る）', '画面遷移', true, '選択した要素が退き、相手へ戻る'],
    ['fade', 'フェードイン／アウト', '画面遷移', false, '透明度を変えて現れ、消える'],
    ['crossfade', 'クロスフェード', '画面遷移', true, '相手と透明度を重ねて入れ替える'],
    ['fadethrough', 'フェードスルー', '画面遷移', true, '相手が消えてから選択した要素が現れる'],
    ['zoom', 'ズーム', '画面遷移', false, '中心から拡大する'],
    ['shared', '共有要素トランジション', '画面遷移', true, '相手の位置・大きさから選択した要素へ移る'],
    ['container', 'コンテナ変形', '画面遷移', true, '相手の枠と角丸から選択した枠へ広がる'],
    ['axis', '共有軸トランジション', '画面遷移', true, '同じ軸で相手から切り替える'],
    ['reveal', 'リビール', '画面遷移', false, '表示する範囲を広げる'],
    ['circular', '円形リビール', '画面遷移', false, '中心から円形に表示する'],
    ['flip', 'フリップ', '画面遷移', true, '選択した表面から相手の裏面へ回転する'],
    ['curl', 'ページカール（簡略）', '画面遷移', false, '端を傾けてめくる。紙の曲面は簡略表現'],
    ['scale', 'スケール', 'UI・スクロール', false, '押し込むように縮んで戻る'],
    ['ripple', 'リップル', 'UI・スクロール', false, '要素内に波紋が広がる'],
    ['bounce', 'バウンス', 'UI・スクロール', false, '跳ねる幅が小さくなって止まる'],
    ['spring', 'スプリング', 'UI・スクロール', false, '行き過ぎてから往復して収まる'],
    ['shake', 'シェイク', 'UI・スクロール', false, '細かく左右に揺れる'],
    ['pulse', 'パルス', 'UI・スクロール', false, '脈打つように拡大する'],
    ['morph', 'モーフィング', 'UI・スクロール', false, 'HTMLの角丸、SVGの矩形、対応するSVGパスの形を変える'],
    ['expand', '展開／折りたたみ', 'UI・スクロール', false, '高さを変えて開閉する'],
    ['stagger', 'スタガー', 'UI・スクロール', false, '直下の子要素を順番に表示する'],
    ['parallax', 'パララックス', 'UI・スクロール', false, '直下の子要素を異なる速さで動かす'],
    ['inertia', '慣性スクロール', 'UI・スクロール', false, 'スクロールできる要素を減速しながら進める'],
    ['overscroll', 'オーバースクロール', 'UI・スクロール', false, '端で伸びて戻る動きを再生する'],
    ['snap', 'スナップ', 'UI・スクロール', false, '少しずれた位置から元の位置へ収まる'],
    ['shimmer', 'シマー', 'UI・スクロール', false, '要素上に明るい帯を流す']
  ].map(function (v) { return { id: v[0], name: v[1], group: v[2], peer: v[3], note: v[4] }; });
  function validate(c) {
    if (typeof c === 'string') { if (c.length > 2000) throw new Error('動きの設定が長すぎます'); c = JSON.parse(c); }
    if (!c || typeof c !== 'object' || Array.isArray(c) || Object.keys(c).some(function (k) { return ['preset', 'duration', 'delay', 'strength', 'direction', 'trigger', 'repeat', 'peer'].indexOf(k) < 0; })) throw new Error('動きの設定が不正です');
    if (!catalog.some(function (p) { return p.id === c.preset; })) throw new Error('未対応の動きです');
    if (!Number.isFinite(c.duration) || c.duration < 100 || c.duration > 30000 || !Number.isFinite(c.delay) || c.delay < 0 || c.delay > 10000 || !Number.isFinite(c.strength) || c.strength < .25 || c.strength > 2) throw new Error('時間か強さが範囲外です');
    if (['left', 'right', 'up', 'down'].indexOf(c.direction) < 0 || ['load', 'click', 'hover', 'scroll'].indexOf(c.trigger) < 0 || typeof c.repeat !== 'boolean') throw new Error('向きか再生条件が不正です');
    if (c.peer != null && (!c.peer || typeof c.peer !== 'object' || Array.isArray(c.peer) || Object.keys(c.peer).some(function (k) { return ['selector', 'tag'].indexOf(k) < 0; }) || typeof c.peer.selector !== 'string' || !c.peer.selector || c.peer.selector.length > 500 || /[\x00-\x1f<{};`]/.test(c.peer.selector) || !/^[a-z][a-z0-9-]{0,40}$/.test(c.peer.tag))) throw new Error('相手の指定が不正です');
    return c;
  }
  function resolvePeer(el, c) {
    if (!c.peer) return null;
    var nodes;
    try { nodes = document.querySelectorAll(c.peer.selector); } catch (e) { throw new Error('相手の指定を読み取れません'); }
    if (nodes.length !== 1 || nodes[0].localName !== c.peer.tag) throw new Error('相手を一意に見つけられません');
    var peer = nodes[0];
    if (peer === el || el.contains(peer) || peer.contains(el)) throw new Error('相手は別の、親子関係にない要素を選んでください');
    return peer;
  }
  function stop(el) {
    var s = active.get(el);
    if (!s) return;
    s.animations.forEach(function (a) { a.cancel(); });
    s.cleanup.forEach(function (fn) { fn(); });
    active.delete(el);
  }
  function play(el, config, event) {
    var c = validate(config), spec = catalog.find(function (p) { return p.id === c.preset; }), peer = resolvePeer(el, c);
    if (spec.peer && !peer) throw new Error('この動きには「相手のオブジェクト」の指定が必要です');
    var b = el.getBoundingClientRect(), p = peer && peer.getBoundingClientRect();
    if (!el.isConnected || b.width <= 0 || b.height <= 0 || (p && (!p.width || !p.height))) throw new Error('表示されている要素を選んでください');
    if (c.preset === 'inertia' && el.scrollHeight <= el.clientHeight && el.scrollWidth <= el.clientWidth) throw new Error('慣性スクロールはスクロールできる枠を選んでください');
    if ((c.preset === 'stagger' || c.preset === 'parallax') && !el.children.length) throw new Error('子要素を持つ枠を選んでください');
    if (c.preset === 'morph' && el instanceof SVGElement && !/^(rect|path)$/.test(el.localName)) throw new Error('SVGのモーフィングは矩形か、相手を指定した同構造のパスを選んでください');
    if (c.preset === 'morph' && el.localName === 'path' && (!peer || peer.localName !== 'path' || (el.getAttribute('d') || '').replace(/[^a-z]/ig, '') !== (peer.getAttribute('d') || '').replace(/[^a-z]/ig, ''))) throw new Error('SVGパスは同じ命令構造の相手パスが必要です');
    stop(el);
    var s = { animations: [], cleanup: [] }; active.set(el, s);
    var duration = c.duration, n = c.strength, sign = /^(right|down)$/.test(c.direction) ? -1 : 1;
    var vertical = /^(up|down)$/.test(c.direction), distance = Math.max(20, vertical ? b.height : b.width) * n;
    function shift(value) { return vertical ? 'translateY(' + value + 'px)' : 'translateX(' + value + 'px)'; }
    function animate(node, frames, extra) {
      var a = node.animate(frames, Object.assign({ duration: duration, delay: c.delay, iterations: c.repeat ? Infinity : 1, easing: 'ease-in-out', fill: 'none', composite: 'add' }, extra || {}));
      s.animations.push(a); return a;
    }
    function transform(node, values, offsets) { return animate(node, values.map(function (value, i) { return { transform: value, offset: offsets ? offsets[i] : i / (values.length - 1) }; })); }
    function fade(node, values, offsets) { return animate(node, values.map(function (value, i) { return { opacity: value, offset: offsets ? offsets[i] : i / (values.length - 1) }; }), { composite: 'replace' }); }
    function overlay(kind) {
      var clip = document.createElement('div'), wave = document.createElement('div');
      clip.setAttribute('data-studio-motion-transient', ''); clip.setAttribute('aria-hidden', 'true');
      Object.assign(clip.style, { position: 'fixed', left: b.left + 'px', top: b.top + 'px', width: b.width + 'px', height: b.height + 'px', overflow: 'hidden', borderRadius: getComputedStyle(el).borderRadius, pointerEvents: 'none', zIndex: '2147483645' });
      if (kind === 'ripple') {
        var size = Math.hypot(b.width, b.height) * 2;
        var x = event && Number.isFinite(event.clientX) ? event.clientX - b.left : b.width / 2;
        var y = event && Number.isFinite(event.clientY) ? event.clientY - b.top : b.height / 2;
        Object.assign(wave.style, { position: 'absolute', width: size + 'px', height: size + 'px', left: x - size / 2 + 'px', top: y - size / 2 + 'px', background: 'rgba(130,170,210,.45)', borderRadius: '50%' });
      } else Object.assign(wave.style, { position: 'absolute', inset: '0', background: 'linear-gradient(100deg,transparent,rgba(255,255,255,.8),transparent)' });
      clip.appendChild(wave); document.body.appendChild(clip); s.cleanup.push(function () { clip.remove(); });
      transform(wave, kind === 'ripple' ? ['scale(0)', 'scale(1)'] : ['translateX(-110%)', 'translateX(110%)']);
      fade(wave, [0, Math.min(1, .6 * n), 0]);
      return clip;
    }
    switch (c.preset) {
      case 'slide': transform(el, [shift(sign * distance), shift(0)]); break;
      case 'push': transform(el, [shift(sign * distance), shift(0)]); transform(peer, [shift(0), shift(-sign * distance)]); break;
      case 'pop': transform(el, [shift(0), shift(sign * distance)]); transform(peer, [shift(-sign * distance * .25), shift(0)]); break;
      case 'fade': fade(el, [0, 1, 1, 0], [0, .3, .7, 1]); break;
      case 'crossfade': fade(peer, [1, 0]); fade(el, [0, 1]); break;
      case 'fadethrough': fade(peer, [1, 0, 0], [0, .4, 1]); fade(el, [0, 0, 1], [0, .4, 1]); transform(el, ['scale(.92)', 'scale(1)']); break;
      case 'zoom': transform(el, ['scale(' + Math.max(.1, 1 - .6 * n) + ')', 'scale(1)']); break;
      case 'shared': case 'container':
        transform(el, ['translate(' + (p.left - b.left) + 'px,' + (p.top - b.top) + 'px) scale(' + p.width / b.width + ',' + p.height / b.height + ')', 'translate(0px,0px) scale(1,1)']);
        var oldOrigin = el.style.transformOrigin; el.style.transformOrigin = '0 0'; s.cleanup.push(function () { el.style.transformOrigin = oldOrigin; });
        fade(peer, [1, 0]);
        if (c.preset === 'container') animate(el, [{ borderRadius: getComputedStyle(peer).borderRadius }, { borderRadius: getComputedStyle(el).borderRadius }], { composite: 'replace' });
        break;
      case 'axis': transform(el, [shift(sign * 40 * n), shift(0)]); transform(peer, [shift(0), shift(-sign * 40 * n)]); fade(el, [0, 1]); fade(peer, [1, 0]); break;
      case 'reveal':
        var hidden = { left: 'inset(0 0 0 100%)', right: 'inset(0 100% 0 0)', up: 'inset(100% 0 0 0)', down: 'inset(0 0 100% 0)' };
        animate(el, [{ clipPath: hidden[c.direction] }, { clipPath: 'inset(0 0 0 0)' }], { composite: 'replace' }); break;
      case 'circular': animate(el, [{ clipPath: 'circle(0% at 50% 50%)' }, { clipPath: 'circle(150% at 50% 50%)' }], { composite: 'replace' }); break;
      case 'flip':
        var axis = vertical ? 'X' : 'Y';
        transform(el, ['perspective(800px) rotate' + axis + '(0deg)', 'perspective(800px) rotate' + axis + '(' + sign * 180 + 'deg)']);
        transform(peer, ['perspective(800px) rotate' + axis + '(' + -sign * 180 + 'deg)', 'perspective(800px) rotate' + axis + '(0deg)']);
        fade(el, [1, 1, 0, 0], [0, .49, .5, 1]); fade(peer, [0, 0, 1, 1], [0, .49, .5, 1]); break;
      case 'curl': animate(el, [{ clipPath: 'inset(0 0 0 0)', transform: 'perspective(800px) rotateY(0deg) skewY(0deg)' }, { clipPath: 'inset(0 100% 0 0)', transform: 'perspective(800px) rotateY(-35deg) skewY(-8deg)' }], { composite: 'replace' }); break;
      case 'scale': transform(el, ['scale(1)', 'scale(' + (1 - .12 * n) + ')', 'scale(1)']); break;
      case 'ripple': case 'shimmer': overlay(c.preset); break;
      case 'bounce': transform(el, [0, -32, 0, -15, 0, -6, 0].map(function (v) { return 'translateY(' + v * n + 'px)'; })); break;
      case 'spring': transform(el, [shift(-80 * n), shift(14 * n), shift(-8 * n), shift(4 * n), shift(0)]); break;
      case 'shake': transform(el, [0, -12, 12, -10, 8, -4, 0].map(function (v) { return 'translateX(' + v * n + 'px)'; })); break;
      case 'pulse': transform(el, [1, 1 + .14 * n, 1, 1 + .14 * n, 1].map(function (v) { return 'scale(' + v + ')'; })); break;
      case 'morph':
        if (el.localName === 'path') animate(el, [{ d: 'path("' + el.getAttribute('d') + '")' }, { d: 'path("' + peer.getAttribute('d') + '")' }], { composite: 'replace' });
        else if (el instanceof SVGElement) animate(el, [{ rx: '0px' }, { rx: Math.min(b.width, b.height) / 2 + 'px' }], { composite: 'replace' });
        else animate(el, [{ borderRadius: getComputedStyle(el).borderRadius }, { borderRadius: peer ? getComputedStyle(peer).borderRadius : '50%' }], { composite: 'replace' });
        break;
      case 'expand': animate(el, [{ height: '0px', overflow: 'hidden' }, { height: b.height + 'px', overflow: 'hidden' }, { height: '0px', overflow: 'hidden' }], { composite: 'replace' }); break;
      case 'stagger': case 'parallax':
        Array.from(el.children).filter(function (node) { return !node.hasAttribute('data-studio-motion-transient'); }).slice(0, 100).forEach(function (node, i, all) {
          if (c.preset === 'stagger') {
            var lag = duration * .5 * i / all.length;
            animate(node, [{ opacity: 0, transform: 'translateY(' + 16 * n + 'px)' }, { opacity: 1, transform: 'translateY(0px)' }], { delay: c.delay + lag, duration: duration * .5, composite: 'replace' });
          } else transform(node, ['translateY(0px)', 'translateY(' + -20 * n * (i + 1) + 'px)']);
        }); break;
      case 'inertia':
        var horizontal = el.scrollHeight <= el.clientHeight, from = horizontal ? el.scrollLeft : el.scrollTop;
        var max = horizontal ? el.scrollWidth - el.clientWidth : el.scrollHeight - el.clientHeight;
        var to = from < max - 1 ? Math.min(max, from + Math.max(50, max * .7 * n)) : 0;
        var clock = { value: null, start: null }, cancelled = false;
        function tick(time) {
          if (cancelled) return;
          if (clock.start == null) clock.start = time + c.delay;
          var t = Math.max(0, Math.min(1, (time - clock.start) / duration)), value = from + (to - from) * (1 - Math.pow(1 - t, 3));
          if (horizontal) el.scrollLeft = value; else el.scrollTop = value;
          if (t < 1 || c.repeat) { if (t === 1) clock.start = time; clock.value = requestAnimationFrame(tick); }
        }
        clock.value = requestAnimationFrame(tick);
        s.cleanup.push(function () { cancelled = true; cancelAnimationFrame(clock.value); if (horizontal) el.scrollLeft = from; else el.scrollTop = from; });
        break;
      case 'overscroll': transform(el, ['translateY(0px) scaleY(1)', 'translateY(' + 18 * n + 'px) scaleY(' + (1 + .05 * n) + ')', 'translateY(-3px) scaleY(1)', 'translateY(0px) scaleY(1)']); break;
      case 'snap': transform(el, [shift(30 * n), shift(3 * n), shift(0)]); break;
    }
    if (!c.repeat && s.animations.length) Promise.all(s.animations.map(function (a) { return a.finished.catch(function () {}); })).then(function () { if (active.get(el) === s) stop(el); });
    return s;
  }
  function unbind(el) { stop(el); var clean = bindings.get(el); if (clean) clean(); bindings.delete(el); }
  function bind(el) {
    unbind(el);
    if (!el.hasAttribute(ATTR) || document.documentElement.getAttribute('data-ss-mode') === 'edit') return;
    var c;
    try { c = validate(el.getAttribute(ATTR)); } catch (e) { return; }
    var cleanup = [], running = false;
    function run(event) { try { play(el, c, event); } catch (e) { el.dispatchEvent(new CustomEvent('studio-motion-error', { detail: e.message })); } }
    if (c.trigger === 'load') run();
    else if (c.trigger === 'scroll') {
      if (c.preset === 'parallax') {
        var scrollPlay = function () {
          if (!el.isConnected) return;
          try {
            if (!active.has(el)) play(el, Object.assign({}, c, { repeat: false }));
            var s = active.get(el), b = el.getBoundingClientRect(), progress = Math.max(0, Math.min(.999, (window.innerHeight - b.top) / (window.innerHeight + b.height)));
            if (s) s.animations.forEach(function (a) { a.pause(); a.currentTime = progress * c.duration; });
          } catch (e) { el.dispatchEvent(new CustomEvent('studio-motion-error', { detail: e.message })); }
        };
        window.addEventListener('scroll', scrollPlay, { passive: true }); scrollPlay(); cleanup.push(function () { window.removeEventListener('scroll', scrollPlay); });
      } else {
        var observer = new IntersectionObserver(function (entries) { entries.forEach(function (entry) { if (entry.isIntersecting && !running) { running = true; run(); } else if (!entry.isIntersecting) running = false; }); });
        observer.observe(el); cleanup.push(function () { observer.disconnect(); });
      }
    } else {
      var type = c.trigger === 'hover' ? 'pointerenter' : 'click';
      el.addEventListener(type, run); cleanup.push(function () { el.removeEventListener(type, run); });
    }
    bindings.set(el, function () { cleanup.forEach(function (fn) { fn(); }); });
  }
  function scan(root) { if (root.nodeType !== 1) return; if (root.hasAttribute(ATTR)) bind(root); root.querySelectorAll('[' + ATTR + ']').forEach(bind); }
  window.ScreenStudioMotion = { catalog: catalog, validate: validate, play: play, stop: stop, stopAll: function () { Array.from(active.keys()).forEach(stop); }, bind: bind };
  function init() {
    scan(document.documentElement);
    new MutationObserver(function (records) {
      records.forEach(function (record) { if (record.type === 'attributes') bind(record.target); else record.addedNodes.forEach(scan); });
      Array.from(bindings.keys()).forEach(function (el) { if (!el.isConnected) unbind(el); });
      Array.from(active.keys()).forEach(function (el) { if (!el.isConnected) stop(el); });
    }).observe(document.documentElement, { subtree: true, childList: true, attributes: true, attributeFilter: [ATTR] });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();

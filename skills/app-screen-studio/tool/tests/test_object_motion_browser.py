"""図鑑の動きの再生、相手選択、保存と製品向けの実行経路を確認する。"""
import json
from pathlib import Path
import unittest
import test_editor_browser as helpers
import studio


@unittest.skipUnless(helpers.sync_playwright, 'ブラウザー試験にはplaywrightが必要')
class ObjectMotionBrowserTests(unittest.TestCase):
    setUpClass = classmethod(helpers.EditorBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(helpers.EditorBrowserTests.tearDownClass.__func__)
    setUp = helpers.EditorBrowserTests.setUp
    tearDown = helpers.EditorBrowserTests.tearDown
    open = helpers.EditorBrowserTests.open

    def test_all_catalog_motions_and_invalid_targets(self):
        self.open()
        frame = self.page.frame_locator('#frame')
        frame.locator('#countBtn').click()
        self.page.locator('#objectMotionBtn').click()
        self.page.wait_for_function("() => document.querySelectorAll('#objectMotionPreset option').length === 28")
        result = frame.locator('body').evaluate('''() => {
          const runtime = window.ScreenStudioMotion;
          const a = document.createElement('div'), b = document.createElement('div');
          a.id='motion-a'; b.id='motion-b';
          a.style.cssText='position:fixed;top:20px;left:20px;width:120px;height:80px;overflow:auto;background:#aaa';
          b.style.cssText='position:fixed;top:120px;left:180px;width:160px;height:100px;background:#bbb';
          a.innerHTML='<div style="height:300px">一</div><div>二</div><div>三</div>';
          document.body.append(a,b);
          const base={duration:2000,delay:0,strength:1,direction:'left',trigger:'click',repeat:false,peer:{selector:'#motion-b',tag:'div'}};
          const results=runtime.catalog.map(p=>{
            const state=runtime.play(a,{...base,preset:p.id});
            const out={id:p.id,animations:state.animations.length,cleanup:state.cleanup.length};
            runtime.stop(a);return out;
          });
          const rejected=[];
          for(const [preset,node,peer] of [['push',a,null],['stagger',b,null],['inertia',b,null],['shared',a,{selector:'#missing',tag:'div'}]]){
            try {runtime.play(node,{...base,preset,peer});}catch(error){rejected.push(preset);}
          }
          return {results,rejected,transients:document.querySelectorAll('[data-studio-motion-transient]').length};
        }''')
        self.assertEqual(len(result['results']), 28)
        self.assertTrue(all(r['animations'] or r['cleanup'] for r in result['results']))
        self.assertEqual(len(result['rejected']), 4)
        self.assertEqual(result['transients'], 0)

    def test_ui_peer_save_undo_reapply_and_click_trigger(self):
        self.open()
        frame = self.page.frame_locator('#frame')
        frame.locator('#countBtn').click()
        self.page.locator('#objectMotionBtn').click()
        self.page.wait_for_function("() => document.querySelectorAll('#objectMotionPreset option').length === 28")
        self.page.locator('#objectMotionPreset').select_option('push')
        self.page.locator('#objectMotionApply').click()
        self.page.wait_for_function("() => document.getElementById('objectMotionStatus').textContent.includes('相手')")
        self.assertIsNone(frame.locator('#countBtn').get_attribute('data-studio-motion'))
        self.page.locator('#objectMotionPeer').click()
        frame.locator('#title').click()
        self.page.wait_for_function("() => document.getElementById('objectMotionPeerName').textContent.includes('title')")
        self.page.locator('#objectMotionPreset').select_option('push')
        self.page.locator('#objectMotionDuration').fill('2')
        self.page.locator('#objectMotionApply').click()
        self.page.wait_for_function("() => document.querySelector('#frame').contentDocument.querySelector('#countBtn').hasAttribute('data-studio-motion')")
        self.page.locator('#objectMotionStop').click()
        self.page.locator('#undoBtn').click()
        self.page.wait_for_function("() => !document.querySelector('#frame').contentDocument.querySelector('#countBtn').hasAttribute('data-studio-motion')")
        self.page.locator('#redoBtn').click()
        self.page.locator('#saveBtn').click()
        self.page.wait_for_function("() => !document.getElementById('copyBtn').disabled")
        records = [json.loads(p.read_text(encoding='utf-8')) for p in Path(self.server.out).rglob('*.json')]
        saved = next(v for v in records if 'edits' in v)
        config = json.loads(saved['edits'][0]['after']['attrs']['data-studio-motion'])
        self.assertEqual(config['preset'], 'push')
        self.assertEqual(config['peer'], {'selector': '[data-edit-id="demo-title"]', 'tag': 'h1'})
        self.page.reload()
        self.page.get_by_role('button', name='画面に再適用').click()
        self.page.wait_for_function("() => document.querySelector('#frame').contentDocument.querySelector('#countBtn').hasAttribute('data-studio-motion')")
        self.page.locator('[data-mode="interact"]').click()
        frame.locator('#countBtn').click()
        self.assertIn('(1)', frame.locator('#countBtn').inner_text())
        self.assertEqual(frame.locator('#countBtn').evaluate('(el)=>el.getAnimations().length'), 1)
        self.assertEqual(frame.locator('#title').evaluate('(el)=>el.getAnimations().length'), 1)
        self.page.locator('[data-mode="edit"]').click()
        self.assertEqual(frame.locator('#countBtn').evaluate('(el)=>el.getAnimations().length'), 0)

    def test_standalone_runtime_triggers_and_cleanup(self):
        self.page.goto(self.server.origin() + '/index.html?__studio_raw=1')
        self.page.add_script_tag(url=self.server.origin() + '/__screen_studio__/object-motion.js')
        results = self.page.locator('body').evaluate('''async () => {
          const a=document.getElementById('countBtn'), runtime=window.ScreenStudioMotion;
          const base={preset:'pulse',duration:2000,delay:0,strength:1,direction:'left',trigger:'click',repeat:false,peer:null};
          const results=[];
          for(const trigger of ['load','click','hover','scroll']){
            a.setAttribute('data-studio-motion',JSON.stringify({...base,trigger}));
            await new Promise(resolve=>setTimeout(resolve,30));
            if(trigger==='click')a.click();
            if(trigger==='hover')a.dispatchEvent(new PointerEvent('pointerenter'));
            await new Promise(resolve=>setTimeout(resolve,80));
            results.push({trigger,count:a.getAnimations().length});
            runtime.stopAll();
          }
          a.setAttribute('data-studio-motion',JSON.stringify({...base,preset:'ripple',repeat:true}));
          await new Promise(resolve=>setTimeout(resolve,30));a.click();
          const before=document.querySelectorAll('[data-studio-motion-transient]').length;
          a.removeAttribute('data-studio-motion');
          await new Promise(resolve=>setTimeout(resolve,30));
          const after=document.querySelectorAll('[data-studio-motion-transient]').length;
          return {results,before,after};
        }''')
        self.assertTrue(all(r['count'] == 1 for r in results['results']), results)
        self.assertEqual(results['before'], 1)
        self.assertEqual(results['after'], 0)


class MotionValidationTests(unittest.TestCase):
    def test_untrusted_motion_data_rejected_and_valid_settings_preserved(self):
        base = {'preset': 'slide', 'duration': 1200, 'delay': 0, 'strength': 1, 'direction': 'left', 'trigger': 'click', 'repeat': False, 'peer': None}
        encoded = json.dumps(base)
        self.assertEqual(studio._clean_attrs({'data-studio-motion': encoded}, 'after')['data-studio-motion'], encoded)
        for change in [{'preset': 'javascript:alert(1)'}, {'preset': []}, {'duration': -1}, {'strength': float('nan')}, {'duration': True}, {'trigger': 'eval'}, {'script': 'alert(1)'}, {'peer': {'selector': '<script>', 'tag': 'div'}}]:
            with self.subTest(change=change), self.assertRaises(studio.StudioError):
                studio._clean_attrs({'data-studio-motion': json.dumps(dict(base, **change))}, 'after')

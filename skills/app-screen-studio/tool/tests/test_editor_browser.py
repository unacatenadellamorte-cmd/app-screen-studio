"""専用のローカル画面で下書きとキャラクター編集を検証する。外部AIへは送信しない。"""
import json
from pathlib import Path
import unittest

from test_studio import Server

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright, 'ブラウザー試験にはplaywrightが必要')
class EditorBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel='msedge', headless=True)
        except Exception:
            cls.playwright.stop()
            raise unittest.SkipTest('試験用Edgeを起動できない環境')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.server = Server(root=str(Path(__file__).resolve().parents[1] / 'demo'))
        self.page = self.browser.new_page(viewport={'width': 1280, 'height': 1000})
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.page.close()
        self.server.close()
        self.assertEqual(self.errors, [])

    def open(self, ai=False):
        if ai:
            def session(route):
                data = route.fetch().json()
                data['ai'] = {'configured': True, 'target': '試験用', 'worktree': '専用試験', 'verified': True}
                route.fulfill(json=data)
            self.page.route('**/api/session', session)
            self.page.route('**/api/ai/status', lambda r: r.fulfill(json={'ok': True, 'ai': {'configured': True, 'verified': True}}))
        self.page.goto(self.server.origin() + '/__screen_studio__/shell')
        self.page.frame_locator('#frame').locator('#demo-mascot').wait_for()
        self.page.wait_for_function("() => document.getElementById('screenPath').textContent.includes('index.html')")

    def test_send_separates_next_draft_and_restores_failures(self):
        self.open(ai=True)
        responses = []
        self.page.route('**/api/ai/send', lambda route: responses.append(route))
        prompt = self.page.locator('#aiPrompt')
        button = self.page.locator('#aiSendBtn')
        for result in [{'ok': True, 'request_file': '試験用'}, {'ok': False, 'message': '試験用の失敗'}, {'ok': False, 'ambiguous': True}]:
            prompt.fill('送る文章\n改行も保持  ')
            button.click()
            self.page.wait_for_function("() => document.getElementById('aiPrompt').value === ''")
            prompt.fill('次の下書き')
            self.page.wait_for_timeout(100)
            self.assertEqual(len(responses), 1)
            route = responses.pop()
            self.assertEqual(route.request.post_data_json['prompt'], '送る文章\n改行も保持  ')
            route.fulfill(json=result)
            self.page.wait_for_function("() => !document.getElementById('aiSendBtn').disabled")
            expected = '次の下書き' if result['ok'] else '送る文章\n改行も保持  \n\n次の下書き'
            self.assertEqual(prompt.input_value(), expected)
        prompt.fill('通信失敗でも残す')
        button.click()
        self.page.wait_for_timeout(100)
        responses.pop().abort('failed')
        self.page.wait_for_function("() => !document.getElementById('aiSendBtn').disabled")
        self.assertEqual(prompt.input_value(), '通信失敗でも残す')
        prompt.press('End')
        prompt.press('Enter')
        self.assertEqual(responses, [])

    def test_character_parts_undo_save_and_reapply(self):
        self.open()
        self.page.locator('#characterBtn').click()
        self.page.wait_for_function("() => document.getElementById('characterPart').options.length === 5")
        self.assertEqual(self.page.locator('#characterTarget').input_value(), '1')
        self.page.locator('#characterPart').select_option('1')
        self.page.wait_for_function("() => !document.getElementById('characterColor').disabled")
        color = self.page.locator('#characterColor')
        color.fill('#ff8844')
        color.dispatch_event('change')
        frame = self.page.frame_locator('#frame')
        body = frame.locator('[data-edit-id="mascot-body"]')
        self.page.wait_for_function("() => document.querySelector('#frame').contentDocument.querySelector('[data-edit-id=mascot-body]').style.fill === 'rgb(255, 136, 68)'")
        self.page.locator('#undoBtn').click()
        self.page.wait_for_function("() => document.querySelector('#frame').contentDocument.querySelector('[data-edit-id=mascot-body]').style.fill === ''")
        self.page.locator('#redoBtn').click()
        self.page.locator('#characterPart').select_option('0')
        self.page.locator('#shiftX').fill('18')
        self.page.locator('#shiftX').press('Tab')
        self.page.locator('[data-prop="width"]').fill('170')
        self.page.locator('[data-prop="width"]').press('Tab')
        self.page.locator('#saveBtn').click()
        self.page.wait_for_function("() => !document.getElementById('copyBtn').disabled")
        files = list(Path(self.server.out).rglob('*.json'))
        saved = [json.loads(p.read_text(encoding='utf-8')) for p in files if 'edits' in json.loads(p.read_text(encoding='utf-8'))][0]
        self.assertEqual(len(saved['edits']), 2)
        self.assertTrue(any(e['target'].get('edit_id') == 'mascot-body' and e['after']['style']['fill'] for e in saved['edits']))
        self.page.reload()
        self.page.get_by_role('button', name='画面に再適用').click()
        self.page.wait_for_function("() => document.querySelector('#frame').contentDocument.querySelector('[data-edit-id=mascot-body]').style.fill === 'rgb(255, 136, 68)'")
        self.assertEqual(self.page.frame_locator('#frame').locator('#demo-mascot').evaluate('(el) => el.style.translate'), '18px')

    def test_manual_image_selection_and_wrong_target_protection(self):
        self.open()
        self.page.locator('#characterBtn').click()
        self.page.locator('#characterPick').click()
        self.page.frame_locator('#frame').locator('#logo').click()
        self.page.wait_for_function("() => !document.getElementById('characterImage').disabled")
        self.assertTrue(self.page.locator('#characterColor').is_disabled())
        import io
        from PIL import Image
        image = io.BytesIO()
        Image.new('RGB', (4, 4), '#4488bb').save(image, format='PNG')
        self.page.locator('#characterImage').click()
        self.page.locator('#imageFile').set_input_files({'name': 'キャラ.png', 'mimeType': 'image/png', 'buffer': image.getvalue()})
        self.page.wait_for_function("() => document.querySelector('#frame').contentDocument.querySelector('#logo').getAttribute('src').includes('/__screen_studio__/assets/')")
        self.page.locator('#undoBtn').click()
        self.page.wait_for_function("() => document.querySelector('#frame').contentDocument.querySelector('#logo').getAttribute('src') === 'logo.svg'")
        self.page.frame_locator('#frame').locator('#title').click()
        self.page.wait_for_function("() => document.getElementById('characterImage').disabled")

    def test_part_motion_presets_speed_pause_and_persistence(self):
        self.open()
        self.page.locator('#characterBtn').click()
        self.page.wait_for_function("() => document.getElementById('characterPart').options.length === 5")
        self.page.locator('#characterPart').select_option('2')
        self.page.wait_for_function("() => !document.getElementById('characterMotion').disabled")
        eye = self.page.frame_locator('#frame').locator('[data-edit-id="mascot-eye-left"]')
        for kind in ['blink', 'talk', 'sway', 'bounce', 'breathe']:
            self.page.locator('#characterMotion').select_option(kind)
            self.page.wait_for_function("() => document.querySelector('#frame').contentDocument.querySelector('[data-edit-id=mascot-eye-left]').style.animationName.startsWith('studio-" + kind + "-')")
            for strength in ['soft', 'medium', 'strong']:
                self.page.locator('#characterStrength').select_option(strength)
                self.page.wait_for_function("() => document.querySelector('#frame').contentDocument.querySelector('[data-edit-id=mascot-eye-left]').style.animationName.endsWith('-" + strength + "')")
                self.assertEqual(eye.evaluate('(el) => el.getAnimations().length'), 1)
        self.page.locator('#characterDuration').fill('1.5')
        self.page.locator('#characterDuration').press('Tab')
        self.page.locator('#characterPlaying').uncheck()
        self.page.wait_for_function("() => document.querySelector('#frame').contentDocument.querySelector('[data-edit-id=mascot-eye-left]').style.animationPlayState === 'paused'")
        self.assertEqual(eye.evaluate('(el) => el.getAnimations()[0].playState'), 'paused')
        self.assertEqual(eye.evaluate('(el) => el.getAnimations()[0].effect.getTiming().duration'), 1500)
        self.assertEqual(self.page.frame_locator('#frame').locator('[data-edit-id="mascot-eye-right"]').evaluate('(el) => el.getAnimations().length'), 0)
        self.page.locator('#saveBtn').click()
        self.page.wait_for_function("() => !document.getElementById('copyBtn').disabled")
        self.page.reload()
        self.page.get_by_role('button', name='画面に再適用').click()
        self.page.wait_for_function("() => document.querySelector('#frame').contentDocument.querySelector('[data-edit-id=mascot-eye-left]').style.animationPlayState === 'paused'")
        self.assertEqual(self.page.frame_locator('#frame').locator('[data-edit-id="mascot-eye-left"]').evaluate('(el) => el.getAnimations()[0].effect.getTiming().duration'), 1500)
        self.page.locator('#aiPrompt').fill('動きも製品へ反映して')
        self.page.locator('#aiExportBtn').click()
        self.page.wait_for_function("() => document.getElementById('aiNote').textContent.includes('依頼文を保存しました')")
        request = next(Path(self.server.out).rglob('*.md')).read_text(encoding='utf-8')
        self.assertIn('character-motion.css', request)


if __name__ == '__main__':
    unittest.main()

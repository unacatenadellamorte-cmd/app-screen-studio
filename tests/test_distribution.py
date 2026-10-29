"""配布先での起動・指示書保存・既存スキル保護を実ファイルで確認する。"""
from pathlib import Path
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'skills/app-screen-studio/tool'
sys.path[:0] = [str(TOOL), str(TOOL / 'tests')]
from test_studio import Server, valid_payload
import studio

spec = importlib.util.spec_from_file_location('studio_installer', ROOT / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

class DistributionTests(unittest.TestCase):
    def test_request_export_without_orca_preserves_prompt_and_validates_json(self):
        server = Server()
        try:
            prompt = '文字を明るく。\n既存の指示を残す。  '
            result = server.state.export_request({'prompt': prompt, 'payload': valid_payload(), 'attachments': []})
            self.assertEqual(result['state'], 'exported')
            self.assertIn(prompt, Path(result['request_file']).read_text(encoding='utf-8'))
            data = json.loads(Path(result['saved_path']).read_text(encoding='utf-8'))
            self.assertEqual(len(data['edits']), 1)
            self.assertIsNone(server.state.bridge)
        finally:
            server.close()

    def test_export_api_blocks_missing_token_and_cross_origin(self):
        server = Server()
        try:
            body = json.dumps({'prompt': '指示', 'payload': valid_payload()}).encode()
            headers = {'Origin': server.origin(), 'Content-Type': 'application/json'}
            self.assertEqual(server.req('POST', studio.PREFIX + '/api/ai/export', body, headers)[0], 403)
            headers['X-Studio-Token'] = server.token()
            headers['Origin'] = 'https://example.invalid'
            self.assertEqual(server.req('POST', studio.PREFIX + '/api/ai/export', body, headers)[0], 403)
            headers['Origin'] = server.origin()
            status, _, response = server.req('POST', studio.PREFIX + '/api/ai/export', body, headers)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(response)['ok'])
        finally:
            server.close()

    def test_bad_attachment_stops_before_writing(self):
        server = Server()
        try:
            with self.assertRaises(studio.StudioError):
                server.state.export_request({'prompt': '指示', 'payload': valid_payload(), 'attachments': ['../missing.png']})
            self.assertFalse(any(Path(server.out).rglob('*.json')))
        finally:
            server.close()

    def test_installed_skill_works_from_another_directory_and_preserves_existing(self):
        with tempfile.TemporaryDirectory(prefix='screen-studio-install-') as directory:
            project = Path(directory) / '日本語 空白'
            destination = installer.install('claude', 'project', project)
            self.assertTrue((destination / 'SKILL.md').is_file())
            tool = destination / 'tool'
            program = ('import sys;sys.path.insert(0,sys.argv[1]);import studio;'
                       's=studio.StudioState("demo","dir",root=studio.DEMO_DIR,output_root=sys.argv[2]);'
                       'assert s.am is not None;print("起動用データを作成")')
            process = subprocess.run([sys.executable, '-X', 'utf8', '-c', program, str(tool), str(Path(directory) / '出力')], cwd=directory, capture_output=True, encoding='utf-8')
            self.assertEqual(process.returncode, 0, process.stderr)
            before = (destination / 'SKILL.md').read_bytes()
            with self.assertRaises(FileExistsError):
                installer.install('claude', 'project', project)
            self.assertEqual((destination / 'SKILL.md').read_bytes(), before)
            self.assertTrue(installer.install('orca', 'project', project).is_dir())
            self.assertTrue(installer.install('codex', 'project', project).is_dir())

    def test_portable_output_environment(self):
        with tempfile.TemporaryDirectory(prefix='screen-studio-output-') as directory:
            env = dict(os.environ, APP_SCREEN_STUDIO_OUTPUT_ROOT=directory)
            process = subprocess.run([sys.executable, '-c', 'import studio;print(studio.DEFAULT_OUTPUT_ROOT)'], cwd=TOOL, env=env, capture_output=True, encoding='utf-8')
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(Path(process.stdout.strip()), Path(directory))

if __name__ == '__main__':
    unittest.main()

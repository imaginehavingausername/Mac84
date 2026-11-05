import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mac'))
import mac_settings as ms


class DummyResult:
    def __init__(self, rc=0, out='', err=''):
        self.returncode, self.stdout, self.stderr = rc, out, err


class SettingsTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(ms, '_supports_ui', return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(ms, 'LEGACY_JOURNALS', ())
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_defaults(self, state):
        def fake_run(*parts):
            if parts[0] == 'killall':
                return DummyResult()
            _, action, domain, key, *args = parts
            ident = (domain, key)
            if action == 'read':
                return DummyResult(out=state[ident] + '\n') if ident in state else DummyResult(rc=1, err='does not exist')
            if action == 'delete':
                if ident not in state:
                    return DummyResult(rc=1, err='not found')
                state.pop(ident)
                return DummyResult()
            if action == 'write':
                state[ident] = args[-1]
                return DummyResult()
            raise AssertionError(parts)
        return fake_run

    def test_restore_preserves_present_and_absent_keys(self):
        state = {
            ('com.apple.menuextra.clock', 'ShowSeconds'): '1',
            ('com.apple.universalaccess', 'reduceMotion'): '0',
        }
        original = state.copy()
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / 'original.json'
            with patch.object(ms, '_run', side_effect=self.fake_defaults(state)):
                with ms.MacSettings(journal=journal):
                    self.assertEqual(state[('com.apple.menuextra.clock', 'ShowSeconds')], 'false')
                    self.assertTrue(journal.exists())
                self.assertFalse(journal.exists())
                self.assertEqual(state.keys(), original.keys())
                for ident in original:
                    self.assertTrue(ms._same(state[ident], original[ident], "-bool"))

    def test_existing_backup_recovers_then_starts(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / 'original.json'
            state = {('com.apple.menuextra.clock', 'ShowSeconds'): 'false'}
            journal.write_text(json.dumps([
                ['com.apple.menuextra.clock', 'ShowSeconds', '-bool', 'true'],
            ]))
            with patch.object(ms, '_run', side_effect=self.fake_defaults(state)):
                with ms.MacSettings(journal=journal):
                    self.assertEqual(state[('com.apple.menuextra.clock', 'ShowSeconds')], 'false')
                self.assertEqual(state[('com.apple.menuextra.clock', 'ShowSeconds')], 'true')
                self.assertFalse(journal.exists())

    def test_external_manual_changes_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / 'original.json'
            state = {('com.apple.menuextra.clock', 'ShowSeconds'): '1'}
            with patch.object(ms, '_run', side_effect=self.fake_defaults(state)):
                with ms.MacSettings(journal=journal):
                    state[('com.apple.menuextra.clock', 'ShowSeconds')] = 'custom'
                self.assertEqual(state[('com.apple.menuextra.clock', 'ShowSeconds')], 'custom')

    def test_legacy_backup_recovery_without_automatic_session(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / 'previous.json'
            state = {('com.apple.menuextra.clock', 'ShowSeconds'): 'false'}
            journal.write_text(json.dumps([
                ['com.apple.menuextra.clock', 'ShowSeconds', '-bool', '1'],
            ]))
            with patch.object(ms, '_run', side_effect=self.fake_defaults(state)):
                ms.restore_settings(journal)
            self.assertTrue(ms._same(state[('com.apple.menuextra.clock', 'ShowSeconds')], '1', '-bool'))
            self.assertFalse(journal.exists())

    def test_restore_failure_preserves_journal(self):
        state = {('com.apple.menuextra.clock', 'ShowSeconds'): 'false'}
        def blocked(*cmd):
            if cmd[:2] == ('defaults', 'write'):
                return DummyResult(rc=1, err='Access denied')
            return self.fake_defaults(state)(*cmd)
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / 'recover.json'
            journal.write_text(json.dumps({ 'entries': [dict(
                domain='com.apple.menuextra.clock',key='ShowSeconds',kind='-bool',
                original='1',temporary='false')]}))
            with patch.object(ms, '_run', side_effect=blocked):
                with self.assertRaisesRegex(RuntimeError, 'Access denied'):
                    ms.restore_settings(journal)
            self.assertTrue(journal.exists())

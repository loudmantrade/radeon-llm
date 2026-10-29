from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from warm_opencode import extract, warm, wait_saved, verify, private_json
from cache_index import packed
import hashlib
import os
from warm_opencode import matches_project


class WarmPrefixTests(unittest.TestCase):
    def test_capture_accepts_native_path_spelling_and_rejects_other_project(self):
        project = Path.cwd() / 'Project with spaces'
        reported = os.path.normcase(str(project)).replace(os.sep, '/')
        self.assertTrue(matches_project('Working directory: ' + reported + '\r\n', project))
        self.assertFalse(matches_project('Working directory: ' + reported + '/other\n', project))

    def test_plan_reminder_is_static_but_user_question_is_excluded(self):
        marker = 'UNIQUE_USER_QUESTION'
        prefix = 's' * 600 + 'plan reminder: user>'
        api = Mock(return_value={'prompt': prefix + marker + '<assistant>'})
        api.tokens.side_effect = lambda s: list(s.encode())
        body = {'messages': [{'role': 'system', 'content': 'instructions'},
                            {'role': 'user', 'content': 'Plan mode reminder'},
                            {'role': 'user', 'content': marker}]}
        self.assertEqual(extract(api, body, marker), list(prefix.encode()))

    def test_history_and_ambiguous_marker_are_rejected(self):
        api = Mock(return_value={'prompt': 'MARKMARK'})
        for messages in [[{'role': 'assistant', 'content': 'history'}, {'role': 'user', 'content': 'MARK'}],
                         [{'role': 'user', 'content': 'MARK'}]]:
            with self.assertRaises(ValueError):
                extract(api, {'messages': messages}, 'MARK')

    def test_warm_uses_no_generation_and_no_experimental_shift(self):
        api = Mock(return_value={'content': '', 'tokens_predicted': 0, 'timings': {'prompt_n': 600}})
        warm(api, [3] * 600)
        body = api.call_args.args[1]
        self.assertEqual(body['n_predict'], 0)
        self.assertEqual(body['n_cache_reuse'], 0)
        api.return_value.update(tokens_predicted=1, content='I')
        self.assertEqual(warm(api, [3] * 600)['native_sampled_tokens'], 1)
        api.return_value['tokens_predicted'] = 2
        with self.assertRaises(RuntimeError): warm(api, [3] * 600)

    def test_disk_confirmation_uses_compatible_token_identity(self):
        tokens = [17] * 600
        expected = hashlib.sha256(b'fingerprint' + packed(tokens)).hexdigest()
        api = Mock(return_value={'snapshots': [{'id': expected, 'compatible': True, 'bytes': 1234}]})
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            private_json(directory / 'latest.json', {'fingerprint': 'fingerprint'})
            self.assertEqual(wait_saved(api, tokens, directory)['snapshot'], expected)

    def test_chat_check_rejects_silent_cache_miss(self):
        api = Mock(return_value={'usage': {'prompt_tokens_details': {'cached_tokens': 590}}})
        body = {'messages': [{'role': 'user', 'content': 'marker'}], 'stream': True}
        with self.assertRaises(RuntimeError): verify(api, body, 600)
        self.assertEqual(body['messages'][0]['content'], 'marker')
        self.assertFalse(api.call_args.args[1]['stream'])


if __name__ == '__main__': unittest.main()

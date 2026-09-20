import io
import json
import unittest
from unittest.mock import patch

import launch


class LauncherTests(unittest.TestCase):
    def response(self, value):
        return io.BytesIO(json.dumps(value).encode())

    def test_reuses_current_or_newer_service(self):
        for version in ('1.7.0', '1.8.0', '2.0.0'):
            with self.subTest(version=version), patch('launch.urllib.request.urlopen',
                    return_value=self.response(dict(application='auction-lab', version=version))):
                self.assertTrue(launch.alive())

    def test_old_service_does_not_launch_or_open_old_dashboard(self):
        for version in ('1.5.0', '1.6.0', None, 'invalid'):
            with self.subTest(version=version), patch('launch.urllib.request.urlopen',
                    return_value=self.response(dict(application='auction-lab', version=version))), \
                    patch('launch.subprocess.Popen') as spawn, patch('launch.webbrowser.open') as browser:
                with self.assertRaisesRegex(RuntimeError, '旧版本'):
                    launch.main()
                spawn.assert_not_called()
                browser.assert_not_called()

    def test_no_service_or_other_application_is_not_reused(self):
        with patch('launch.urllib.request.urlopen', side_effect=OSError('offline')):
            self.assertFalse(launch.alive())
        with patch('launch.urllib.request.urlopen', return_value=self.response(dict(application='other'))):
            self.assertFalse(launch.alive())


if __name__ == '__main__':
    unittest.main()

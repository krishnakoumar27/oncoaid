import json
import unittest
from unittest.mock import patch
from intelligence import SarvamError
from languages import command_for, language_choice, localize, PROTECTED


def translated_response(method, route, **kwargs):
    return {'translated_text':'தமிழ் '+kwargs['json']['input']}



class LanguageTests(unittest.TestCase):
    def test_money_equations_commands_and_layout_are_outside_model(self):
        text='Treatment: ₹800,000\nSupport: ₹500,000 (62.50%)\nYou may need: ₹800,000 − ₹500,000 = ₹300,000\n/continue'
        with patch('languages.sarvam',side_effect=translated_response) as provider:
            result=localize(text,'ta-IN')
        self.assertEqual(PROTECTED.findall(result),PROTECTED.findall(text))
        self.assertEqual(result.count('\n'),text.count('\n'))
        sent=' '.join(call.kwargs['json']['input'] for call in provider.call_args_list)
        self.assertNotIn('800,000',sent)
        self.assertNotIn('/continue',sent)
        self.assertTrue(result.startswith('தமிழ்'))

    def test_invalid_translation_and_outage_preserve_original_figures(self):
        text='You may need: ₹300,000. /continue'
        wrong={'translated_text':'Pay ₹0'}
        with patch('languages.sarvam',return_value=wrong):
            result=localize(text,'hi-IN')
            self.assertIn(text,result)
        with patch('languages.sarvam',side_effect=SarvamError('offline')):
            self.assertIn(text,localize(text,'ta-IN'))

    def test_native_shortcuts_and_language_number_do_not_conflict(self):
        self.assertEqual(command_for('தொடரவும்'),'/continue')
        self.assertEqual(command_for('वापस'),'/back')
        self.assertEqual(command_for('continue where I left off'),'/continue')
        self.assertEqual(command_for('/menu@CareBot'),'/menu')
        self.assertEqual(language_choice('३',numbered=True),'hi-IN')
        self.assertIsNone(language_choice('3',numbered=False))

    def test_english_needs_no_provider_request(self):
        with patch('languages.sarvam') as provider:
            self.assertEqual(localize('File received.','en-IN'),'File received.')
            provider.assert_not_called()


if __name__=='__main__':
    unittest.main()

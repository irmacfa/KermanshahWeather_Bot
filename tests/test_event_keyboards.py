"""Regression tests for event inline-keyboard callback contracts."""

import unittest

from bots.telegram_bot import (
    build_event_attendees_text,
    event_attendees_keyboard,
    event_keyboard,
    events_list_keyboard,
    manage_attendees_keyboard,
    manage_event_keyboard,
)


class EventKeyboardTests(unittest.TestCase):
    def _callbacks(self, markup):
        return [button.callback_data for row in markup.inline_keyboard for button in row]

    def test_public_event_buttons_have_distinct_actions(self):
        callbacks = self._callbacks(event_keyboard(42, 3))
        self.assertIn('ev:a|42', callbacks)
        self.assertIn('ev:c|42', callbacks)
        self.assertIn('ev:v|42|0', callbacks)
        self.assertIn('ev:n|42', callbacks)

    def test_event_picker_opens_event_detail_not_attendee_page(self):
        callbacks = self._callbacks(events_list_keyboard([{'event_id': 42, 'title': 'پرواز جمعه'}]))
        self.assertEqual(callbacks[0], 'ev:s|42')

    def test_attendee_pages_have_navigation_and_back_button(self):
        first = self._callbacks(event_attendees_keyboard(42, 0, 31))
        second = self._callbacks(event_attendees_keyboard(42, 1, 31))
        self.assertIn('ev:v|42|1', first)
        self.assertIn('ev:v|42|0', second)
        self.assertIn('ev:b|42', first)

    def test_admin_event_menu_exposes_expected_actions(self):
        active = self._callbacks(manage_event_keyboard(42, 1))
        closed = self._callbacks(manage_event_keyboard(42, 0))
        self.assertIn('me:att|42|0', active)
        self.assertIn('me:ren|42', active)
        self.assertIn('me:close|42', active)
        self.assertIn('me:open|42', closed)
        self.assertIn('me:del|42', active)

    def test_admin_attendee_page_is_clamped_after_last_attendee_removed(self):
        attendees = [
            {'user_id': i, 'first_name': f'User{i}', 'last_name': 'Test',
             'username': None, 'platform': 'telegram'}
            for i in range(1, 10)
        ]
        callbacks = self._callbacks(manage_attendees_keyboard(42, attendees, page=99))
        self.assertIn('me:att|42|0', callbacks)
        self.assertFalse(any(value == 'me:att|42|99' for value in callbacks))

    def test_attendee_management_callback_payloads(self):
        attendees = [
            {'user_id': 7, 'first_name': 'Ali', 'last_name': 'Test', 'username': None, 'platform': 'telegram'}
        ]
        callbacks = self._callbacks(manage_attendees_keyboard(42, attendees, 0))
        self.assertTrue(any(value.startswith('me:an|42|7|0|') for value in callbacks))
        self.assertTrue(any(value.startswith('me:ar|42|7|0|') for value in callbacks))

    def test_attendee_text_escapes_names_and_clamps_page(self):
        event = {'event_id': 42, 'title': '<b>Test</b>', 'platform': 'telegram'}
        attendees = [
            {'first_name': '<b>Injected</b>', 'last_name': 'Name', 'username': None, 'platform': 'telegram'}
        ]
        text = build_event_attendees_text(event, attendees, page=99)
        self.assertIn('&lt;b&gt;Test&lt;/b&gt;', text)
        self.assertIn('&lt;b&gt;Injected&lt;/b&gt;', text)
        self.assertIn('صفحه 1 از 1', text)

    def test_callback_data_stays_within_platform_limit(self):
        attendees = [
            {'user_id': 123456789012345678, 'first_name': 'Ali', 'last_name': 'Test', 'username': None, 'platform': 'telegram'}
        ]
        markups = [
            event_keyboard(123456789012345678, 1),
            event_attendees_keyboard(123456789012345678, 0, 100),
            manage_event_keyboard(123456789012345678, 1),
            manage_attendees_keyboard(123456789012345678, attendees, 0),
        ]
        for markup in markups:
            for callback in self._callbacks(markup):
                self.assertLessEqual(len(callback.encode('utf-8')), 64)


if __name__ == '__main__':
    unittest.main()
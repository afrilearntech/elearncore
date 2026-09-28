import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings

from elearncore.sysutils.constants import UserRole
from messsaging.invitations import build_invitation_payload, queue_account_invitation
from messsaging.models import NotificationDelivery
from messsaging.tasks import _deliver_channel_once, send_account_notifications_task


class NotificationDeliveryTests(TestCase):
    def test_successful_channel_is_not_delivered_twice(self):
        key = str(uuid.uuid4())
        calls = []

        def deliver():
            calls.append('sent')
            return {'status': 'accepted'}

        self.assertTrue(_deliver_channel_once(key, 'sms', '+231770000000', deliver))
        self.assertFalse(_deliver_channel_once(key, 'sms', '+231770000000', deliver))
        self.assertEqual(calls, ['sent'])
        delivery = NotificationDelivery.objects.get(idempotency_key=key, channel='sms')
        self.assertEqual(delivery.status, NotificationDelivery.Status.SENT)
        self.assertEqual(delivery.attempts, 1)


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    DEFAULT_FROM_EMAIL='LR Digital Leanrning Platform <notifications@example.com>',
    FRONTEND_BASE_URL='https://learn.example',
    TEMPORARY_PASSWORD_TTL_HOURS=24,
)
class AccountInvitationTests(TestCase):
    def make_user(self, role=UserRole.TEACHER.value):
        return SimpleNamespace(
            name='Pat Doe',
            email='pat@example.com',
            phone='+231770000101',
            role=role,
        )

    def test_invitation_email_is_branded_and_contains_temporary_credentials(self):
        user = self.make_user()
        payload = build_invitation_payload(user, 'Temp-Pass-42')
        key = str(uuid.uuid4())

        result = send_account_notifications_task.run(
            key,
            payload['message'],
            None,
            user.email,
            payload['subject'],
            payload['email_context'],
        )

        self.assertEqual(result, {'delivered': ['email']})
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(
            email.from_email,
            'LR Digital Leanrning Platform <notifications@example.com>',
        )
        self.assertEqual(email.to, ['pat@example.com'])
        self.assertIn('Teacher account', email.subject)
        self.assertIn('Temp-Pass-42', email.body)
        self.assertEqual(email.alternatives[0].mimetype, 'text/html')
        html = email.alternatives[0].content
        self.assertIn('Digital Learning Platform', html)
        self.assertIn('https://learn.example/parent-teacher/sign-in/teacher', html)
        self.assertIn('Temp-Pass-42', html)
        delivery = NotificationDelivery.objects.get(idempotency_key=key, channel='email')
        self.assertEqual(delivery.status, NotificationDelivery.Status.SENT)

    def test_each_user_role_gets_the_correct_login_page(self):
        expected_paths = {
            UserRole.ADMIN.value: '/admin/sign-in',
            UserRole.CONTENTCREATOR.value: '/content/sign-in',
            UserRole.CONTENTVALIDATOR.value: '/content/sign-in',
            UserRole.TEACHER.value: '/parent-teacher/sign-in/teacher',
            UserRole.HEADTEACHER.value: '/parent-teacher/sign-in/teacher',
            UserRole.PARENT.value: '/parent-teacher/sign-in/parent',
            UserRole.STUDENT.value: '/login',
        }

        for role, path in expected_paths.items():
            with self.subTest(role=role):
                payload = build_invitation_payload(self.make_user(role), 'Temporary-1')
                self.assertEqual(
                    payload['email_context']['login_url'],
                    f'https://learn.example{path}',
                )

    @patch('messsaging.tasks.send_account_notifications_task.apply_async')
    def test_queue_includes_sms_email_and_structured_email_context(self, apply_async):
        user = self.make_user(UserRole.STUDENT.value)

        queue_account_invitation(user, 'Temporary-2')

        apply_async.assert_called_once()
        call_args = apply_async.call_args.kwargs['args']
        self.assertEqual(call_args[2], user.phone)
        self.assertEqual(call_args[3], user.email)
        self.assertEqual(call_args[5]['temporary_password'], 'Temporary-2')
        self.assertEqual(call_args[5]['login_url'], 'https://learn.example/login')
        self.assertFalse(apply_async.call_args.kwargs['retry'])
        self.assertEqual(
            apply_async.call_args.kwargs['argsrepr'],
            '(<redacted account invitation>)',
        )

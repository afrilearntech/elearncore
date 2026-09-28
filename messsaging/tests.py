import uuid

from django.test import TestCase

from messsaging.models import NotificationDelivery
from messsaging.tasks import _deliver_channel_once

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

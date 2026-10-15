"""Connection cleanup with real offer parsing; all traffic is offline."""
import copy
import html
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent / 'tests'), str(Path(__file__).resolve().parents[1])]
import app
from funpay import FunPayError
from test_connection_modes import ConnectionFixtureClient, KEY, seller_trade
from test_funpay_client import editor as new_editor
from test_management_client import editor as old_editor
import test_management_jobs


class CleanupClient(ConnectionFixtureClient):
    def __init__(self, offers, *, include_summary=False):
        super().__init__(new_editor=new_editor())
        self.offers = copy.deepcopy(offers)
        self.delete_error = None
        self.before_offer = None
        self.complete = True
        self.include_summary = include_summary
        self.on_save = None

    def trade_row(self, oid, spec):
        summary = self.include_summary
        active = spec.get('summary_active', spec['active'])
        amount = spec.get('summary_amount', spec['amount'])
        status = ' warning' if summary and active is False else ''
        quantity = f'<div class="tc-amount">{html.escape(str(amount))}</div>' if summary and amount is not None else ''
        return (f'<a class="tc-item{status}" data-offer="{oid}" href="/lots/offerEdit?offer={oid}&amp;node=123">'
                f'<div class="tc-desc-text">Test {oid}</div><div class="tc-price"><span class="unit">₽</span></div>'
                f'{quantity}</a>')

    def _request(self, path, payload=None, *, readonly=False):
        if path == '/lots/123/trade':
            self.calls.append((path, None, readonly))
            source = seller_trade(0).replace('</div></body>', ''.join(
                self.trade_row(oid, spec) for oid, spec in self.offers.items()) + '</div></body>')
            return source if self.complete else source.replace('class="tc table-hover table-clickable"', 'class="missing"')
        if path.startswith('/lots/offerEdit?offer='):
            self.calls.append((path, None, readonly)); self.edits += 1
            oid = path.split('offer=', 1)[1].split('&', 1)[0]
            if self.before_offer:
                self.before_offer(oid)
            spec = self.offers[oid]
            source = old_editor(offer=oid, auto=False, amount=spec['amount'], token=f'token-{self.edits}')
            if spec['active'] is False:
                source = source.replace('name="active" checked', 'name="active"')
            return source
        if path == '/lots/offerSave':
            self.calls.append((path, dict(payload), readonly))
            if self.on_save:
                self.on_save()
            if self.delete_error:
                raise self.delete_error
            assert payload['deleted'] == '1'
            del self.offers[payload['offer_id']]
            return json.dumps({'done': True})
        return super()._request(path, payload, readonly=readonly)


class CleanupJobTests(unittest.TestCase):
    request = test_management_jobs.ManagementJobsTests.request

    def wait(self, instance, job_id):
        deadline = time.monotonic() + 3
        while True:
            result = instance.get_connection_job(job_id)
            if result['state'] != 'running':
                return result
            self.assertLess(time.monotonic(), deadline, 'Offline cleanup did not finish')
            time.sleep(.005)

    def launch(self, client):
        instance = app.Lister(client_factory=lambda: client)
        status, result = self.request(instance, 'POST', '/api/connect', {'golden_key': KEY, 'category': '123'})
        self.assertEqual(status, 200, result)
        return instance, result['connection_job_id']

    def test_deletes_paused_and_zero_only_before_opening_new_editor(self):
        client = CleanupClient({'111': {'active': False, 'amount': 5}, '112': {'active': True, 'amount': 0}, '113': {'active': True, 'amount': 5}})
        with patch.object(app, 'CLEANUP_DELAY', 0):
            instance, jid = self.launch(client); job = self.wait(instance, jid)
        self.assertEqual(job['state'], 'completed', job)
        self.assertEqual((job['deleted'], job['skipped']), (2, 1))
        self.assertEqual(set(client.offers), {'113'})
        self.assertTrue(job['result']['creation_available'])
        paths = [path for path, _, _ in client.calls]
        self.assertGreater(paths.index('/lots/offerEdit?node=123'), max(i for i,p in enumerate(paths) if p == '/lots/offerSave'))
        self.assertEqual(len(client.saves), 2)
        self.assertNotIn(KEY, json.dumps(job))
        for payload in client.saves:
            self.assertEqual(set(payload), {'offer_id','node_id','csrf_token','form_created_at','deleted'})

    def test_reactivated_listing_is_kept_by_fresh_editor_check(self):
        client = CleanupClient({'111': {'active': False, 'amount': 5}}, include_summary=True)
        client.before_offer = lambda oid: client.offers[oid].update(active=True)
        instance, jid = self.launch(client); job = self.wait(instance, jid)
        self.assertEqual((job['deleted'], job['skipped']), (0, 1), job)
        self.assertEqual(client.saves, [])
        self.assertEqual(client.edits, 1)

    def test_complete_active_positive_summary_skips_all_40_editors_and_deletions(self):
        client = CleanupClient({str(i+111): {'active': True, 'amount': 1} for i in range(40)}, include_summary=True)
        waits = []
        with patch.object(threading.Event, 'wait', autospec=True, side_effect=lambda _event, timeout=None: waits.append(timeout) or False):
            instance = app.Lister(client_factory=lambda: client)
            info = instance.connect({'golden_key': KEY, 'category': '123'})
        job = instance.get_connection_job(instance.connection_job['job_id'])
        self.assertEqual(job['state'], 'completed', job)
        self.assertEqual((job['deleted'], job['skipped']), (0, 40))
        self.assertEqual(client.edits, 0)
        self.assertEqual(client.saves, [])
        self.assertFalse(info['creation_available'])
        self.assertNotIn('/lots/offerEdit?node=123', [path for path, _, _ in client.calls])
        self.assertEqual(waits, [])

    def test_unknown_summary_quantity_still_uses_fresh_owned_editor(self):
        client = CleanupClient({'111': {'active': True, 'amount': 5, 'summary_amount': None}}, include_summary=True)
        instance, jid = self.launch(client)
        job = self.wait(instance, jid)
        self.assertEqual((job['deleted'], job['skipped']), (0, 1), job)
        self.assertEqual(client.edits, 1)
        self.assertEqual(client.saves, [])

    def test_inspections_that_keep_unknown_quantity_do_not_wait_three_seconds(self):
        client = CleanupClient({'111': {'active': True, 'amount': 5, 'summary_amount': None},
                                '112': {'active': True, 'amount': 3, 'summary_amount': None}}, include_summary=True)
        waits = []
        with patch.object(threading.Event, 'wait', autospec=True, side_effect=lambda _event, timeout=None: waits.append(timeout) or False):
            instance = app.Lister(client_factory=lambda: client)
            instance.connect({'golden_key': KEY, 'category': '123'})
        self.assertEqual(client.edits, 2)
        self.assertEqual(client.saves, [])
        self.assertEqual(waits, [])

    def test_three_second_spacing_applies_between_actual_deletion_posts(self):
        client = CleanupClient({'111': {'active': False, 'amount': 5},
                                '112': {'active': True, 'amount': 3},
                                '113': {'active': True, 'amount': 0}}, include_summary=True)
        now, waits, post_times = [100.0], [], []

        def paced_wait(_event, timeout=None):
            waits.append(timeout)
            now[0] += timeout or 0
            return False

        client.on_save = lambda: post_times.append(now[0])
        with patch.object(app, 'CLEANUP_DELAY', 3), \
                patch.object(app.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(threading.Event, 'wait', autospec=True, side_effect=paced_wait):
            instance = app.Lister(client_factory=lambda: client)
            info = instance.connect({'golden_key': KEY, 'category': '123'})
        self.assertEqual(info['cleanup']['deleted'], 2)
        self.assertEqual(info['cleanup']['skipped'], 1)
        self.assertEqual(client.edits, 2)
        self.assertEqual(post_times, [100.0, 103.0])
        self.assertEqual(waits, [3])

    def test_never_deletes_active_stocked_listings_to_force_cap(self):
        client = CleanupClient({str(i+111): {'active': True, 'amount': 1} for i in range(40)})
        with patch.object(app, 'CLEANUP_DELAY', 0):
            instance, jid = self.launch(client); job = self.wait(instance, jid)
        self.assertEqual(job['deleted'], 0, job)
        self.assertEqual(len(client.offers), 40)
        self.assertFalse(job['result']['creation_available'])
        self.assertNotIn('/lots/offerEdit?node=123', [p for p,_,_ in client.calls])

    def test_partial_category_page_aborts_before_any_deletion(self):
        client = CleanupClient({'111': {'active': False, 'amount': 1}}); client.complete = False
        instance, jid = self.launch(client); job = self.wait(instance, jid)
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(client.saves, [])
        self.assertIsNone(instance.connection)

    def test_unconfirmed_deletion_stops_without_retry_or_remaining_writes(self):
        client = CleanupClient({'111': {'active': False, 'amount': 1}, '112': {'active': False, 'amount': 1}})
        client.delete_error = FunPayError('Save unknown', kind='network', uncertain=True)
        instance, jid = self.launch(client); job = self.wait(instance, jid)
        self.assertEqual(job['state'], 'stopped', job)
        self.assertEqual([row['state'] for row in job['items']], ['uncertain','cancelled'])
        self.assertEqual(len(client.saves), 1)
        self.assertFalse(job['result']['creation_available'])
        self.assertIn('111', client.offers)

    def test_stop_after_current_keeps_later_listings_and_rejects_other_operations(self):
        client = CleanupClient({'111': {'active': True, 'amount': 1}, '112': {'active': False, 'amount': 1}})
        entered, release = threading.Event(), threading.Event()
        def block(oid):
            entered.set(); self.assertTrue(release.wait(2))
        client.before_offer = block
        instance, jid = self.launch(client)
        self.assertTrue(entered.wait(2))
        try:
            status, _ = self.request(instance, 'POST', '/api/connect_stop', {'job_id':jid}, token=False)
            self.assertEqual(status,403)
            with self.assertRaises(app.RequestError): instance.disconnect()
            with self.assertRaises(app.RequestError): instance.begin_connect({'golden_key':KEY,'category':'456'})
            status, _ = self.request(instance, 'POST', '/api/connect_stop', {'job_id':jid})
            self.assertEqual(status,200)
        finally: release.set()
        job = self.wait(instance,jid)
        self.assertEqual(job['state'],'stopped')
        self.assertEqual(client.saves,[])
        self.assertEqual(job['items'][1]['state'],'cancelled')
        self.assertFalse(instance.busy())

    def test_job_reads_require_token_and_never_expose_key(self):
        client = CleanupClient({})
        instance, jid = self.launch(client); self.wait(instance,jid)
        status, _ = self.request(instance,'GET','/api/connection_job?id='+jid,token=False)
        self.assertEqual(status,403)
        status, job = self.request(instance,'GET','/api/connection_job?id='+jid)
        self.assertEqual(status,200)
        self.assertNotIn(KEY,json.dumps(job))


if __name__ == '__main__': unittest.main()

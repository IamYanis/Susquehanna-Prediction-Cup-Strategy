"""Mocked Colorado add-on recovery; every state write is in a temp directory."""
import copy
import time
import unittest
from decimal import Decimal
from urllib.parse import urlparse
from unittest.mock import patch

import autonomous_pilot as auto
import live_pilot as pilot
import pilot_account
import recover_filled_addon_leg1 as recovery
import recover_filled_leg1 as filled
import test_autonomous_lifecycle as lifecycle
from test_account_reader import holdings, orders_page
from test_api_audit import exchange_book
from test_supervised_accounting_probe import zero_collateral


class ColoradoFixture(unittest.TestCase):
    def setUp(self):
        self.life = lifecycle.AutonomousLifecycleTests()
        self.addCleanup(self.life.doCleanups)
        self.life.setUp()
        self.fixture, self.session, self.path = self.life.fixture, self.life.session, self.life.fixture.path
        for sequence in (1, 2, 3):
            self.books(sequence)
            result = self.life.cycle()
            self.assertEqual(result["state"], pilot.READY, result.get("reason"))
        self.parent = next(iter(self.fixture.checkpoint()["autonomous_positions"]))
        self.original = copy.deepcopy(self.fixture.checkpoint())
        self.assertEqual(self.original["autonomous_positions"][self.parent]["quantity"], 3)
        self.books(4)

    def books(self, sequence):
        for mid, eid, price in ((1, 11, ".075"), (2, 12, ".900")):
            book = exchange_book(mid, eid, bid=float(Decimal(1) - Decimal(price)), ask=.995)
            book["asOf"]["sequence"] = sequence
            self.fixture.books[str(eid)] = book


class LegSnapshotTests(ColoradoFixture):
    def test_delayed_terminal_get_uses_new_account_clock_without_duplicate_gets(self):
        clock = time.monotonic
        offset, windows = [0], []
        observe, snapshot = auto.single.observe_test, pilot_account.read_snapshot

        def delayed(session, intent, activity=None):
            result = observe(session, intent, activity=activity)
            cp = self.fixture.checkpoint()
            if cp["state"] == pilot.LEG1_RECONCILING:
                offset[0] += 20  # The pre-trade account snapshot is now stale.
            return result

        def protected(session, *args, **kwargs):
            index = len(self.session.get.call_args_list)
            result = snapshot(session, *args, **kwargs)
            windows.append((kwargs.get("order_ids"), self.session.get.call_args_list[index:]))
            return result

        self.session.get.reset_mock()
        with patch.object(pilot_account.time, "monotonic", side_effect=lambda: clock() + offset[0]), \
             patch.object(auto.single, "observe_test", side_effect=delayed), \
             patch.object(pilot_account, "read_snapshot", side_effect=protected):
            result = self.life.cycle()
        self.assertEqual(result["state"], pilot.READY, result.get("reason"))
        self.assertEqual(self.fixture.checkpoint()["autonomous_positions"][self.parent]["quantity"], 4)
        self.assertTrue(windows)
        for ids, calls in windows:
            self.assertEqual(ids, [])
            self.assertFalse(any('/orders/' in call.args[0] for call in calls))
        # Leg one is read once for reconciliation and once at finalization;
        # leg two is read once. None occurs inside a protected account window.
        for oid, count in ((1007, 2), (1008, 1)):
            for path in (f'/orders/{oid}', f'/orders/{oid}/fills'):
                self.assertEqual(sum(urlparse(c.args[0]).path.endswith(path)
                                     for c in self.session.get.call_args_list), count)

    def assert_changed_snapshot_halts(self, kind):
        snapshot = pilot_account.read_snapshot
        def changed(session, *args, **kwargs):
            result = snapshot(session, *args, **kwargs)
            cp = kwargs.get('checkpoint') or (args[1] if len(args) > 1 else None)
            if cp and cp['state'] == pilot.LEG1_RECONCILING:
                result = copy.deepcopy(result)
                if kind == 'cash':
                    result['account']['tournament']['myBalance'] -= 1
                elif kind == 'holding':
                    result['account']['positions'][0]['quantity'] -= 1
                else:
                    result['recent_transactions'].append(dict(result['recent_transactions'][0], event_id='unexpected'))
            return result
        with patch.object(pilot_account, 'read_snapshot', side_effect=changed):
            result = self.life.cycle()
        self.assertEqual(result['state'], pilot.HALTED)
        attempt = auto.active_attempt(self.fixture.checkpoint())
        self.assertEqual(len(self.life.posts), 7)
        self.assertIsNone(attempt['legs'][1]['intent'])

    def test_changed_cash_halts_before_leg_two(self):
        self.assert_changed_snapshot_halts('cash')

    def test_changed_holdings_halt_before_leg_two(self):
        self.assert_changed_snapshot_halts('holding')

    def test_changed_financial_history_halts_before_leg_two(self):
        self.assert_changed_snapshot_halts('history')


class FilledAddonRecoveryTests(ColoradoFixture):
    def setUp(self):
        super().setUp()
        snapshot = pilot_account.read_snapshot
        def stale(session, *args, **kwargs):
            cp = kwargs.get('checkpoint') or (args[1] if len(args) > 1 else None)
            if cp and cp['state'] == pilot.LEG1_RECONCILING:
                raise pilot_account.AccountReadinessBlocked(pilot_account.STALE,
                    'Account read exceeded the existing 15-second freshness window')
            return snapshot(session, *args, **kwargs)
        post = self.life.post
        def ordinary_post(url, **kwargs):
            response = post(url, **kwargs)
            receipt = response.json()
            receipt['all'] = zero_collateral(receipt['fillPrice'])
            return self.fixture.base.response(receipt)
        self.session.post.side_effect = ordinary_post
        with patch.object(pilot_account, 'read_snapshot', side_effect=stale):
            result = self.life.cycle()
        self.assertEqual(result['reason'], recovery.HALT_REASON)
        self.cp = self.fixture.checkpoint()
        self.key = self.cp['autonomous_execution']['active_attempt']
        attempt = auto.active_attempt(self.cp)
        target = {'attempt':self.key, 'parent':self.parent, 'revision':self.cp['revision'],
                  'markets':['1','2'], 'exchanges':['11','12'], 'order':1007, 'fill':2007,
                  'transaction':'trade-2007', 'key':attempt['legs'][0]['intent']['request']['idempotencyKey'],
                  'price':Decimal('.075'), 'baseline_quantity':3, 'baseline_costs':[Decimal('.225'),Decimal('2.700')],
                  'cash':Decimal(str(self.fixture.fixture.current['tournament']['myBalance']))}
        patcher = patch.object(recovery, 'TARGET', target)
        patcher.start(); self.addCleanup(patcher.stop)
        get = self.fixture.get
        def all_orders(url, **kwargs):
            if urlparse(url).path.endswith('/orders') and kwargs.get('params',{}).get('status')=='all':
                return self.fixture.base.response(orders_page([r[0] for r in self.fixture.fixture.receipts.values()],
                                                               coverage={'complete':True}))
            return get(url, **kwargs)
        self.session.get.side_effect = all_orders
        self.session.post.reset_mock()
        self.session.post.side_effect = AssertionError('Recovery cannot POST')

    def test_dry_run_leaves_state_and_completed_position_unchanged(self):
        before = self.path.read_bytes()
        report = recovery.recover(self.session,self.key)
        self.assertEqual(report['result'],'DRY_RUN_PASS')
        self.assertEqual(report['after']['state'],pilot.LEG2_RECHECK)
        self.assertEqual(report['after']['revision'],self.cp['revision']+1)
        self.assertEqual(report['managed_quantity_after'],3)
        self.assertIsNone(report['leg_two_intent'])
        self.assertIsNone(report['resume_consumed_at'])
        self.assertEqual(self.path.read_bytes(),before)
        self.session.post.assert_not_called(); self.session.delete.assert_not_called()

    def test_apply_to_temp_state_preserves_history_and_retires_first_key(self):
        recovery.recover(self.session,self.key,apply=True)
        saved = pilot._read_checkpoint_locked(self.path.resolve())
        old, current = auto.active_attempt(self.cp), auto.active_attempt(saved)
        self.assertEqual(saved['autonomous_positions'],self.cp['autonomous_positions'])
        self.assertEqual(current['legs'][0]['intent'],old['legs'][0]['intent'])
        self.assertEqual(current['legs'][0]['receipt'],old['legs'][0]['receipt'])
        self.assertEqual(current['reads'][:len(old['reads'])],old['reads'])
        self.assertEqual(current['stages'][:len(old['stages'])],old['stages'])
        self.assertEqual(current['filled_leg1_recovery']['submission_status'],'RETIRED_NEVER_RETRY')
        self.assertIsNone(current['legs'][1]['intent'])
        self.assertEqual(saved['live_exposures'][self.key]['confirmed_quantities'],['1','0'])
        self.assertEqual(saved['quarantine_reserve'],self.cp['quarantine_reserve'])
        filled.require_resume(saved,self.key)
        self.session.post.assert_not_called(); self.session.delete.assert_not_called()

    def test_explicit_continuation_submits_only_leg_two_and_merges_three_to_four(self):
        recovery.recover(self.session,self.key,apply=True)
        self.session.post.side_effect = self.life.post
        result = auto.resume_recovered_leg1(self.session,self.key)
        self.assertEqual(result['state'],pilot.READY,result.get('reason'))
        self.assertEqual(self.session.post.call_count,1)
        self.assertEqual(self.session.post.call_args.kwargs['json']['exchangeId'],'12')
        self.assertEqual(len(self.life.posts),8)
        saved = self.fixture.checkpoint()
        p = saved['autonomous_positions'][self.parent]
        self.assertEqual(p['quantity'],4)
        self.assertEqual(p['remaining_quantities'],['4','4'])
        self.assertEqual(Decimal(p['total_entry_cost']),Decimal('3.900'))
        self.assertEqual(list(map(Decimal,p['average_entry_prices'])),[Decimal('.075'),Decimal('.900')])
        self.assertEqual(p['execution_ids'][:-1],self.cp['autonomous_positions'][self.parent]['execution_ids'])
        self.assertIsNotNone(saved['autonomous_execution']['attempts'][self.key]['filled_leg1_recovery']['resume_consumed_at'])
        with self.assertRaises(ValueError): auto.resume_recovered_leg1(self.session,self.key)
        self.assertEqual(self.session.post.call_count,1)

    def test_recovered_execution_cannot_submit_leg_one_or_reuse_its_key(self):
        recovery.recover(self.session,self.key,apply=True)
        saved = pilot._read_checkpoint_locked(self.path.resolve())
        with self.assertRaises(ValueError): auto.submit_once(self.session,None,{'checkpoint':saved},0,lambda:None)
        hex_key = recovery.TARGET['key'].removeprefix('account-test-')
        with patch.object(auto,'uuid4') as uuid:
            uuid.return_value.hex=hex_key
            with self.assertRaises(ValueError): auto.new_intent(saved,auto.active_attempt(saved)['authorization'],1,.9)
        self.session.post.assert_not_called()

    def test_changed_cash_holdings_transactions_or_fills_refuse_without_writes(self):
        before = self.path.read_bytes()
        account = self.fixture.fixture
        selected = next(p for p in account.current['positions'] if p['exchangeId']=='11')
        other = next(p for p in account.current['positions'] if p['exchangeId']=='12')
        cases=[(account.current['tournament'],'myBalance',account.current['tournament']['myBalance']-.05),
               (selected,'quantity',-5), (other,'quantity',-4),
               (account.transactions[0],'price',.08), (account.fills[0],'quantity',-.5)]
        for container,field,value in cases:
            old=container[field]
            try:
                container[field]=value
                with self.subTest(field=field),self.assertRaises(ValueError): recovery.recover(self.session,self.key,apply=True)
                self.assertEqual(self.path.read_bytes(),before)
            finally: container[field]=old
        self.session.post.assert_not_called()

    def test_duplicate_or_added_financial_activity_refuses(self):
        before=self.path.read_bytes(); account=self.fixture.fixture
        for rows,extra in ((account.fills,dict(account.fills[0])),
                           (account.transactions,dict(account.transactions[0],event_id='extra-trade'))):
            rows.append(extra)
            try:
                with self.assertRaises(ValueError): recovery.recover(self.session,self.key,apply=True)
                self.assertEqual(self.path.read_bytes(),before)
            finally: rows.pop()
        self.session.post.assert_not_called()

    def test_partial_or_missing_terminal_fill_refuses(self):
        before=self.path.read_bytes()
        order,fill_page=self.fixture.fixture.receipts[1007]
        order['quantityFilled']=.5
        with self.assertRaises(ValueError): recovery.recover(self.session,self.key,apply=True)
        order['quantityFilled']=1
        fill_page['data']=[]
        with self.assertRaises(ValueError): recovery.recover(self.session,self.key,apply=True)
        self.assertEqual(self.path.read_bytes(),before)
        self.session.post.assert_not_called()

    def test_changed_authorization_or_stale_fresh_snapshot_refuses(self):
        before=self.path.read_bytes()
        with patch.object(pilot,'read_live_pair',side_effect=auto.live.LiveSettlementBlocked('Changed settlement evidence')):
            with self.assertRaises(ValueError): recovery.recover(self.session,self.key,apply=True)
        with patch.object(pilot_account,'read_snapshot',side_effect=pilot_account.AccountReadinessBlocked(pilot_account.STALE,'stale')):
            with self.assertRaises(ValueError): recovery.recover(self.session,self.key,apply=True)
        self.assertEqual(self.path.read_bytes(),before)
        self.session.post.assert_not_called()

    def test_plain_state_save_cannot_clear_the_halt(self):
        cp=copy.deepcopy(self.cp)
        cp.update(state=pilot.LEG2_RECHECK,manual_review_required=False,review_reason='')
        with self.assertRaises(ValueError): pilot.save_checkpoint(cp)
        self.assertEqual(self.fixture.checkpoint()['revision'],self.cp['revision'])

    def test_recovery_uses_utc_chronology_across_process_monotonic_clocks(self):
        recovery.recover(self.session,self.key,apply=True)
        saved = pilot._read_checkpoint_locked(self.path.resolve())
        attempt = copy.deepcopy(auto.active_attempt(saved))
        record = attempt['filled_leg1_recovery']
        # A different process/boot can have a completely different monotonic
        # origin; UTC still proves that these reads bracket the known fill.
        attempt['before']['freshness']['started_monotonic'] += 10000
        attempt['before']['freshness']['completed_monotonic'] += 10000
        self.assertEqual(recovery.prove_fill(attempt,record['snapshot'],record['orders'],record['observed_intent']),record['review'])
        attempt['before']['freshness']['observed_at']='2999-01-01T00:00:00+00:00'
        with self.assertRaises(ValueError):
            recovery.prove_fill(attempt,record['snapshot'],record['orders'],record['observed_intent'])


if __name__=='__main__':
    unittest.main()

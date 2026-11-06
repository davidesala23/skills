#!/usr/bin/env python3
"""Credential-free Discord collection tests, using isolated state and a fake GET client."""

import datetime as dt
import importlib.util
import json
import shutil
import subprocess
from unittest import mock
import os
import sys
import tempfile
from pathlib import Path
import unittest

SCRIPT = str(Path(__file__).resolve().parents[1] / "scripts" / "discord-fetch.py")
sys.path.insert(0, str(Path(SCRIPT).parent))
_spec = importlib.util.spec_from_file_location("discord_fetch", SCRIPT)
df = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(df)

GUILD = "100000000000000001"
PERIMETER = {
    "expected_user_id": "123", "guild": {"id": GUILD, "name": "Example"},
    "channels": [{"id": "555", "name": "#channel", "categoria": "General", "attivo": True}],
    "dm": {"include": True, "include_groups": True, "active_within_days": 7},
    "seed_days": 7, "include_threads": True, "max_messages_per_channel": 500,
}
_fixture_root = None
_fixture_env = None


def setUpModule():
    global _fixture_root, _fixture_env
    _fixture_root = tempfile.TemporaryDirectory()
    root = Path(_fixture_root.name)
    perimeter = root / "perimeter.json"
    perimeter.write_text(json.dumps(PERIMETER))
    example = Path(SCRIPT).parents[1] / "config.example.json"
    config = json.loads(example.read_text())
    config.update(workspace_root=str(root), enabled_sources=["discord"],
                  discord_config_path=str(perimeter), discord_runtime_dir=str(root / "state"),
                  discord_retention_days=30, discord_deadline_seconds=20)
    config_dir = root / "config"
    (config_dir / "ingest").mkdir(parents=True)
    (config_dir / "ingest" / "config.json").write_text(json.dumps(config))
    _fixture_env = mock.patch.dict(os.environ, {"SKILLS_CONFIG_DIR": str(config_dir)})
    _fixture_env.start()
    df.configure_runtime(df.load_ingest_config())


def tearDownModule():
    if _fixture_env:
        _fixture_env.stop()
    if _fixture_root:
        _fixture_root.cleanup()


def msg(**kw):
    base = {"id": "999", "t": "2026-08-24T09:15:00.000000+00:00", "a": "Author",
            "bot": False, "c": "ciao", "att": [], "emb": 0, "ref": None, "men": False}
    base.update(kw)
    return base


def block(kind="channel", **kw):
    base = {"kind": kind, "id": "555", "name": "#canale", "cat": "General",
            "capped": False, "cursor": "999", "messages": [msg()]}
    base.update(kw)
    return base


class TestPermalink(unittest.TestCase):
    def test_channel_uses_guild_id(self):
        url = df.permalink(GUILD, block("channel", id="555"), "999")
        self.assertEqual(url, f"https://discord.com/channels/{GUILD}/555/999")

    def test_dm_uses_at_me(self):
        self.assertIn("/channels/@me/", df.permalink(GUILD, block("dm", id="777"), "12"))

    def test_group_uses_at_me(self):

        self.assertIn("/channels/@me/", df.permalink(GUILD, block("group", id="888"), "12"))

    def test_thread_uses_guild_id_not_at_me(self):


        url = df.permalink(GUILD, block("thread", id="100000000000000009"), "42")
        self.assertEqual(url, f"https://discord.com/channels/{GUILD}/100000000000000009/42")

    def test_an_unknown_kind_degrades_to_the_guild_not_to_at_me(self):
        self.assertIn(GUILD, df.permalink(GUILD, block("kind-che-non-esiste-ancora"), "6"))


class TestRenderHarvest(unittest.TestCase):
    def render(self, results, skipped=None):
        return df.render_harvest({"results": results, "skipped": skipped or []},
                                 "2026-08-24", GUILD)

    def test_dms_come_before_channels(self):
        out = self.render([block("channel", name="#zeta"), block("dm", name="Member", id="7")])
        self.assertLess(out.index("DM — Member"), out.index("CANALE — #zeta"))

    def test_thread_block_is_labelled_and_comes_last(self):
        out = self.render([block("thread", name="#bugs \u203a Usage calcolato male", id="9"),
                           block("channel", name="#zeta")])
        self.assertIn("THREAD — #bugs \u203a Usage calcolato male", out)
        self.assertLess(out.index("CANALE — #zeta"), out.index("THREAD — #bugs"))

    def test_empty_block_is_omitted(self):
        out = self.render([block(messages=[])])
        self.assertNotIn("#canale", out)

    def test_mention_is_flagged(self):
        out = self.render([block(messages=[msg(men=True)])])
        self.assertIn("TI MENZIONA", out)

    def test_bot_and_attachments_and_reply_are_flagged(self):
        out = self.render([block(messages=[
            msg(bot=True, att=["spec.pdf"], ref="member", emb=2)])])
        for expected in ("bot", "allegati: spec.pdf", "risponde a member", "2 embed"):
            self.assertIn(expected, out)

    def test_multiline_body_is_kept_whole(self):


        out = self.render([block(messages=[msg(c="riga uno\nriga due\nriga tre")])])
        for line in ("riga uno", "riga due", "riga tre"):
            self.assertIn(line, out)

    def test_capped_block_says_so(self):
        out = self.render([block(capped=True)])
        self.assertIn("TRONCATO", out)

    def test_skipped_sources_are_named_never_dropped(self):
        out = self.render([block()], skipped=[{"name": "#bugs", "why": "http-403"}])
        self.assertIn("Non letti", out)
        self.assertIn("#bugs — http-403", out)

    def test_every_message_carries_a_permalink(self):
        out = self.render([block(messages=[msg(id="1"), msg(id="2")])])
        self.assertEqual(out.count("https://discord.com/channels/"), 2)


class TestCursors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = df.CURSOR_PATH, df.STATE_DIR
        df.STATE_DIR = self.tmp
        df.CURSOR_PATH = os.path.join(self.tmp, "discord-cursors.json")

    def tearDown(self):
        df.CURSOR_PATH, df.STATE_DIR = self._old

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(df.load_cursors(), {})

    def test_corrupt_file_degrades_to_reread_not_to_skip(self):


        with open(df.CURSOR_PATH, "w") as f:
            f.write("{not json")
        self.assertEqual(df.load_cursors(), {})

    def test_round_trip(self):
        df.save_cursors({"555": "999"})
        self.assertEqual(df.load_cursors(), {"555": "999"})

    def test_save_is_atomic_no_tmp_left_behind(self):
        df.save_cursors({"a": "1"})
        self.assertFalse(os.path.exists(df.CURSOR_PATH + ".tmp"))


class TestRetention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = df.HARVEST_DIR
        df.HARVEST_DIR = self.tmp

    def tearDown(self):
        df.HARVEST_DIR = self._old

    def write(self, name):
        with open(os.path.join(self.tmp, name), "w") as f:
            f.write("x")

    def test_prunes_older_than_retention_keeps_the_rest(self):
        today = dt.date.today()
        old = (today - dt.timedelta(days=df.RETENTION_DAYS + 1)).isoformat()
        edge = (today - dt.timedelta(days=df.RETENTION_DAYS)).isoformat()
        self.write(f"{old}.md")
        self.write(f"{edge}.md")
        self.write(f"{today.isoformat()}.md")
        self.assertEqual(df.prune_harvests(), 1)
        left = sorted(os.listdir(self.tmp))
        self.assertEqual(left, sorted([f"{edge}.md", f"{today.isoformat()}.md"]))

    def test_ignores_files_that_are_not_dated_harvests(self):
        self.write("notes.md")
        self.write("2026-13-99.md")
        self.assertEqual(df.prune_harvests(), 0)
        self.assertEqual(len(os.listdir(self.tmp)), 2)

    def test_missing_dir_is_not_an_error(self):
        df.HARVEST_DIR = os.path.join(self.tmp, "nope")
        self.assertEqual(df.prune_harvests(), 0)


class TestHealth(unittest.TestCase):
    """Outage duration counts days rather than collection attempts."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = df.HEALTH_PATH, df.STATE_DIR
        df.STATE_DIR = self.tmp
        df.HEALTH_PATH = os.path.join(self.tmp, "discord-health.json")

    def tearDown(self):
        df.HEALTH_PATH, df.STATE_DIR = self._old

    def test_missing_file_reads_as_healthy(self):
        self.assertEqual(df.load_health()["consecutive_down"], 0)
        self.assertIsNone(df.load_health()["down_since"])

    def test_corrupt_file_reads_as_healthy_not_as_a_long_outage(self):


        with open(df.HEALTH_PATH, "w") as f:
            f.write("{not json")
        self.assertEqual(df.load_health()["consecutive_down"], 0)

    def test_first_down_starts_the_streak_and_stamps_the_day(self):
        h = df.note_down("2026-09-01")
        self.assertEqual(h["consecutive_down"], 1)
        self.assertEqual(h["down_since"], "2026-09-01")

    def test_two_runs_the_same_day_count_as_one_day(self):


        df.note_down("2026-09-01")
        h = df.note_down("2026-09-01")
        self.assertEqual(h["consecutive_down"], 1)

    def test_consecutive_days_accumulate(self):
        for d in ("2026-09-01", "2026-09-02", "2026-09-03"):
            h = df.note_down(d)
        self.assertEqual(h["consecutive_down"], 3)
        self.assertEqual(h["down_since"], "2026-09-01")

    def test_one_success_clears_the_outage(self):
        df.note_down("2026-09-01")
        df.note_down("2026-09-02")
        df.note_up("2026-09-03")
        h = df.load_health()
        self.assertEqual(h["consecutive_down"], 0)
        self.assertIsNone(h["down_since"])
        self.assertEqual(h["last_ok"], "2026-09-03")

    def test_streak_restarts_from_one_after_a_recovery(self):
        df.note_down("2026-09-01")
        df.note_up("2026-09-02")
        h = df.note_down("2026-09-03")
        self.assertEqual(h["consecutive_down"], 1)
        self.assertEqual(h["down_since"], "2026-09-03")

    def test_save_is_atomic_no_tmp_left_behind(self):
        df.note_down("2026-09-01")
        self.assertFalse(os.path.exists(df.HEALTH_PATH + ".tmp"))


class TestT3Ingest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.multiple(df, STATE_DIR=self.tmp.name,
            CURSOR_PATH=os.path.join(self.tmp.name, 'discord-cursors.json'),
            HEALTH_PATH=os.path.join(self.tmp.name, 'discord-health.json'),
            HARVEST_DIR=os.path.join(self.tmp.name, 'discord'))
        self.patch.start()
        self.intent = {'run_id': 'a' * 32, 'date': dt.date.today().isoformat(), 'dry_run': False,
                       'config': {'expected_user_id': '123', 'guild_id': GUILD,
                                  'channels': [{'id': '555', 'name': '#canale'}]}}
        df.atomic_write(df.run_path(self.intent['run_id']), json.dumps(self.intent))
        self.result = {'run_id': self.intent['run_id'], 'ok': True, 'user_id': '123',
                       'guild_id': GUILD, 'results': [block()], 'skipped': [], 'deadlineHit': False}

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_retry_is_idempotent_and_cursors_never_go_back(self):
        first, code = df.ingest_t3(self.result, self.intent)
        self.assertEqual(code, 0)
        body = Path(first['harvest']).read_bytes()
        df.save_cursors({'555': '1200', '777': '300'})
        second, code = df.ingest_t3(self.result, self.intent)
        self.assertEqual(first, second)
        self.assertEqual(body, Path(second['harvest']).read_bytes())
        self.assertEqual(df.load_cursors(), {'555': '1200', '777': '300'})

    def test_new_run_with_stale_cursor_snapshot_preserves_newer_state(self):
        df.save_cursors({'555': '1200', '777': '300'})
        df.ingest_t3(self.result, self.intent)
        self.assertEqual(df.load_cursors(), {'555': '1200', '777': '300'})

    def test_failed_harvest_never_advances_cursors(self):
        actual = df.atomic_write
        def fail_harvest(path, content):
            if path.endswith('.md'):
                raise OSError('disk full')
            return actual(path, content)
        with mock.patch.object(df, 'atomic_write', side_effect=fail_harvest):
            with self.assertRaises(OSError):
                df.ingest_t3(self.result, self.intent)
        self.assertEqual(df.load_cursors(), {})
        self.assertFalse(os.path.exists(df.HEALTH_PATH))

    def test_cursor_failure_leaves_durable_harvest_and_retry_recovers(self):
        actual = df.atomic_write
        def fail_cursor(path, content):
            if path == df.CURSOR_PATH:
                raise OSError('disk full')
            return actual(path, content)
        with mock.patch.object(df, 'atomic_write', side_effect=fail_cursor):
            with self.assertRaises(OSError):
                df.ingest_t3(self.result, self.intent)
        harvests = list(Path(df.HARVEST_DIR).glob('*.md'))
        self.assertEqual(len(harvests), 1)
        self.assertIn('ciao', harvests[0].read_text())
        summary, _ = df.ingest_t3(self.result, self.intent)
        self.assertEqual(len(list(Path(df.HARVEST_DIR).glob('*.md'))), 1)
        self.assertEqual(df.load_cursors(), {'555': '999'})

    def test_auth_failure_is_down_and_does_not_advance(self):
        res = {'run_id': self.intent['run_id'], 'ok': False, 'why': 'authentication-expired'}
        summary, code = df.ingest_t3(res, self.intent)
        self.assertEqual((summary['status'], code), ('down', 1))
        self.assertEqual(df.load_cursors(), {})
        self.assertFalse(os.path.isdir(df.HARVEST_DIR))
        self.assertEqual(df.load_health()['consecutive_down'], 1)

    def test_partial_empty_is_explicit_and_does_not_clear_outage(self):
        df.note_down(self.intent['date'])
        self.result.update(results=[], skipped=[{'name': '#canale', 'why': 'http-403'}])
        summary, code = df.ingest_t3(self.result, self.intent)
        self.assertEqual((summary['status'], code), ('partial', 0))
        self.assertEqual(df.load_health()['consecutive_down'], 1)

    def test_verified_quiet_day_is_up(self):
        self.result['results'] = [block(messages=[], cursor=None)]
        summary, code = df.ingest_t3(self.result, self.intent)
        self.assertEqual((summary['status'], summary['total'], code), ('up', 0, 0))
        self.assertIsNone(summary['harvest'])

    def test_dry_run_touches_no_health_harvest_or_cursor(self):
        before = sorted(Path(self.tmp.name).rglob('*'))
        df.ingest_t3(self.result, self.intent, dry_run=True)
        self.assertEqual(before, sorted(Path(self.tmp.name).rglob('*')))

    def test_simultaneous_processes_preserve_both_cursor_updates(self):
        other = json.loads(json.dumps(self.intent))
        other['run_id'] = 'b' * 32
        other['config']['channels'] = [{'id': '777', 'name': '#other'}]
        df.atomic_write(df.run_path(other['run_id']), json.dumps(other))
        res = dict(self.result, run_id=other['run_id'], results=[block(id='777')])
        paths = []
        for n, result in enumerate([self.result, res]):
            path = os.path.join(self.tmp.name, str(n) + '.json')
            Path(path).write_text(json.dumps(result))
            paths.append(path)
        processes = [subprocess.Popen([sys.executable, SCRIPT, '--state-dir', self.tmp.name,
                     'ingest-t3', intent['run_id'], '--input', path, '--json'],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for intent, path in zip([self.intent, other], paths)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr + stdout)
        self.assertEqual(df.load_cursors(), {'555': '999', '777': '999'})
        self.assertEqual(len(list(Path(df.HARVEST_DIR).glob('*.md'))), 2)

    def test_malformed_message_does_not_write_state(self):
        self.result['results'][0]['messages'] = [42]
        with self.assertRaisesRegex(ValueError, 'message-shape'):
            df.ingest_t3(self.result, self.intent)
        self.assertEqual(df.load_cursors(), {})
        self.assertFalse(os.path.isdir(df.HARVEST_DIR))

    def test_wrong_account_and_missing_channel_fail_validation(self):
        self.result['user_id'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'identity'):
            df.ingest_t3(self.result, self.intent)
        self.result['user_id'] = '123'
        self.result['results'] = []
        with self.assertRaisesRegex(ValueError, 'channel-missing'):
            df.ingest_t3(self.result, self.intent)
        self.assertEqual(df.load_cursors(), {})


@unittest.skipUnless(shutil.which('node'), 'Node needed for page boundary tests')
class TestT3PageJob(unittest.TestCase):
    def config(self):
        return {'expected_user_id': '123', 'guild_id': GUILD, 'channels': [],
                'dm': {'include': False}, 'cursors': {}, 'seed_days': 7,
                'deadline_seconds': 10, 'include_threads': False,
                'thread_search_max_pages': 4, 'max_messages_per_channel': 500}

    def run_page(self, cfg, get_body, second_cfg=None):
        expressions = [df.t3_start_expression('a' * 32, cfg)]
        if second_cfg:
            expressions.append(df.t3_start_expression('b' * 32, second_cfg))
        script = r"""
const fs=require('fs');const input=JSON.parse(fs.readFileSync(0,'utf8'));
global.window={};global.location={origin:'https://discord.com'};
const calls=[];const http={get:async request=>{calls.push(request);return await onGET(request);}};
const onGET=eval('('+input.get_body+')');
const req=()=>({Bo:http});req.m={636537:true};
const chunks=[];chunks.push=entry=>{entry[2](req);return 1;};window.webpackChunkdiscord_app=chunks;
(async()=>{for(const expression of input.expressions)eval(expression);
const until=Date.now()+12000;
while(Object.values(window.__ingestDiscordJobs).some(j=>!j.done)&&Date.now()<until)await new Promise(r=>setTimeout(r,2));
console.log(JSON.stringify({jobs:window.__ingestDiscordJobs,calls}));})();
"""
        p = subprocess.run(['node', '-e', script], input=json.dumps({'expressions': expressions, 'get_body': get_body}),
                           text=True, capture_output=True, timeout=15)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_expired_session_cannot_become_empty_success(self):
        out = self.run_page(self.config(), "async request=>{throw {status:401};}")
        job = out['jobs']['a' * 32]
        self.assertTrue(job['done'])
        self.assertEqual(job['err'], 'authentication-expired')
        self.assertIsNone(job['out'])

    def test_native_identity_success_and_failed_dm_enumeration_remain_distinct(self):
        cfg = self.config()
        cfg['dm'] = {'include': True, 'include_groups': True, 'active_within_days': 7}
        out = self.run_page(cfg, """async ({url})=>{
if(url==='/users/@me')return {status:200,body:{id:'123'}};
if(url==='/users/@me/guilds')return {status:200,body:[{id:'100000000000000001'}]};
throw {status:403};}""")
        result = out['jobs']['a' * 32]['out']
        self.assertTrue(result['ok'])
        self.assertEqual(result['results'], [])
        self.assertEqual(result['skipped'], [{'name': '(elenco DM)', 'why': 'http-403'}])
        self.assertTrue(all(set(call) <= {'url', 'query'} for call in out['calls']))

    def test_transient_network_failure_retries_without_calling_it_logout(self):
        out = self.run_page(self.config(), """(()=>{let attempts=0;return async ({url})=>{
if(url==='/users/@me' && attempts++<2)throw Error('Request has been terminated');
if(url==='/users/@me')return {status:200,body:{id:'123'}};
return {status:200,body:[{id:'100000000000000001'}]};};})()""")
        self.assertTrue(out['jobs']['a' * 32]['out']['ok'])
        self.assertEqual(sum(c['url'] == '/users/@me' for c in out['calls']), 3)

    def test_retry_wait_cannot_exceed_the_job_deadline(self):
        cfg = self.config()
        cfg['deadline_seconds'] = 0.05
        out = self.run_page(cfg, "async request=>{throw Error('offline');}")
        self.assertEqual(len(out['calls']), 1)
        self.assertTrue(out['jobs']['a' * 32]['done'])
        self.assertIsNone(out['jobs']['a' * 32]['out'])

    def test_concurrent_jobs_keep_their_own_result(self):
        cfg = self.config()
        out = self.run_page(cfg, """async ({url})=>url==='/users/@me'
?{status:200,body:{id:'123'}}:{status:200,body:[{id:'100000000000000001'}]}""", second_cfg=cfg)
        for run_id in ('a' * 32, 'b' * 32):
            self.assertEqual(out['jobs'][run_id]['out']['run_id'], run_id)
            self.assertTrue(out['jobs'][run_id]['done'])

    def test_short_thread_page_with_more_does_not_hide_later_page(self):
        cfg = self.config()
        cfg.update(channels=[{'id': '555', 'name': '#c', 'categoria': ''}], include_threads=True)
        out = self.run_page(cfg, """async ({url,query})=>{
if(url==='/users/@me')return {status:200,body:{id:'123'}};
if(url==='/users/@me/guilds')return {status:200,body:[{id:'100000000000000001'}]};
if(url.endsWith('/threads/search'))return {status:200,body:{has_more:query.offset==='0',threads:[{
id:query.offset==='0'?'1000':'2000',last_message_id:String((BigInt(Date.now())-1420070400000n)<<22n),name:'thread'}]}};
return {status:200,body:[]};}""")
        result = out['jobs']['a' * 32]['out']
        self.assertTrue({'1000', '2000'} <= {item['id'] for item in result['results']})
        self.assertTrue(any(call.get('query', {}).get('offset') == '25' for call in out['calls']))

    def test_already_read_last_thread_does_not_hide_later_page(self):
        cfg = self.config()
        cfg.update(channels=[{'id': '555', 'name': '#c', 'categoria': ''}], include_threads=True, thread_search_max_pages=1)
        cfg['cursors'] = {str(n): '999' for n in range(1000, 1025)}
        out = self.run_page(cfg, """async ({url,query})=>{
if(url==='/users/@me')return {status:200,body:{id:'123'}};
if(url==='/users/@me/guilds')return {status:200,body:[{id:'100000000000000001'}]};
if(url.endsWith('/threads/search'))return {status:200,body:{threads:query.offset==='0'
?Array.from({length:25},(_,i)=>({id:String(1000+i),last_message_id:'999',name:'read'}))
:[{id:'2000',name:'unread',last_message_id:String((BigInt(Date.now())-1420070400000n)<<22n)}]}};
return {status:200,body:[]};}""")
        result = out['jobs']['a' * 32]['out']
        self.assertIn('2000', [b['id'] for b in result['results']])
        self.assertTrue(any(c.get('query', {}).get('offset') == '25' for c in out['calls']))


class TestT3WindowConfig(unittest.TestCase):
    SINCE = '2026-10-27T10:19:50Z'
    UNTIL = '2026-11-03T10:19:50Z'

    def test_window_normalizes_offsets_and_rejects_ambiguous_bounds(self):
        w = df.parse_window('2026-10-27T11:19:50+01:00', self.UNTIL)
        self.assertEqual(w['since'], '2026-10-27T10:19:50.000Z')
        for a, b in [(self.SINCE, None), (None, self.UNTIL),
                     ('2026-10-27T10:19:50', self.UNTIL),
                     (self.UNTIL, self.SINCE), (self.SINCE, self.SINCE),
                     ('2026-10-27', self.UNTIL)]:
            with self.subTest(a=a, b=b), self.assertRaises(ValueError):
                df.parse_window(a, b)

    def test_bounded_config_does_not_even_read_cursors(self):
        with mock.patch.object(df, 'load_cursors', side_effect=AssertionError('cursor read')):
            cfg = df.make_job_config(df.load_config(), 30,
                                     df.parse_window(self.SINCE, self.UNTIL), True)
        self.assertEqual(cfg['cursors'], {})
        self.assertTrue(cfg['include_archived_threads'])

    def test_cli_requires_isolation_before_writing_intent(self):
        for args in [[], ['--state-dir', df.DEFAULT_STATE_DIR]]:
            p = subprocess.run([sys.executable, SCRIPT, *args, 'prepare-t3',
                                '--since', self.SINCE, '--until', self.UNTIL],
                               capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('isolated-state-dir', p.stdout)

    def test_cli_uses_caller_config_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = df.load_config()
            cfg['channels'] = [dict(cfg['channels'][0])]
            cfg['max_messages_per_channel'] = 1200
            path = Path(tmp, 'perimeter.json')
            original = json.dumps(cfg)
            path.write_text(original)
            p = subprocess.run([sys.executable, SCRIPT, '--state-dir', str(Path(tmp, 'state')),
                'prepare-t3', '--since', self.SINCE, '--until', self.UNTIL,
                '--config', str(path), '--include-archived-threads', '--json'],
                capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
            job = json.loads(p.stdout)
            intent = json.loads(Path(tmp, 'state', 't3-runs', job['run_id'] + '.json').read_text())
            self.assertEqual(intent['config']['max_messages_per_channel'], 1200)
            self.assertEqual(len(intent['config']['channels']), 1)
            self.assertEqual(path.read_text(), original)
            self.assertFalse(Path(tmp, 'state', 'discord-cursors.json').exists())


@unittest.skipUnless(shutil.which('node'), 'Node needed for page boundary tests')
class TestT3WindowPageJob(unittest.TestCase):
    def config(self):
        cfg = TestT3PageJob.config(self)
        cfg.update(window=df.parse_window('2026-11-02T00:00:00Z', '2026-11-03T00:00:00Z'),
                   channels=[{'id': '555', 'name': '#weekly', 'categoria': 'Product'}],
                   include_archived_threads=False)
        return cfg

    def run_page(self, cfg, get_body, second_cfg=None):
        return TestT3PageJob.run_page(self, cfg, get_body, second_cfg)

    def body(self, messages_js, extra=''):
        return """async ({url,query})=>{
const sf=ms=>String((BigInt(ms)-1420070400000n)<<22n);
const message=ms=>({id:sf(ms),timestamp:new Date(ms).toISOString(),content:'body',author:{username:'a'},mentions:[],attachments:[],embeds:[]});
const lower=Date.parse('2026-11-02T00:00:00Z'),upper=Date.parse('2026-11-03T00:00:00Z');
if(url==='/users/@me')return {status:200,body:{id:'123'}};
if(url==='/users/@me/guilds')return {status:200,body:[{id:'100000000000000001'}]};
""" + extra + """
if(url.endsWith('/messages')){const ms=""" + messages_js + """;
return {status:200,body:ms.filter(m=>BigInt(m.id)<BigInt(query.before)).sort((a,b)=>BigInt(a.id)>BigInt(b.id)?-1:1).slice(0,100)};}
return {status:200,body:[]};}"""

    def result(self, output):
        job = output['jobs']['a' * 32]
        self.assertIsNone(job['err'])
        return job['out']

    def test_exact_bounds_and_existing_cursor_do_not_skip_messages(self):
        cfg = self.config()
        cfg['cursors'] = {'555': '9999999999999999999'}
        out = self.run_page(cfg, self.body('[message(lower-1),message(lower),message(upper-1),message(upper)]'))
        result = self.result(out)
        times = [m['t'] for m in result['results'][0]['messages']]
        self.assertEqual(times, ['2026-11-02T00:00:00.000Z', '2026-11-02T23:59:59.999Z'])
        requests = [r for r in out['calls'] if r['url'].endswith('/messages')]
        self.assertTrue(all('before' in r['query'] and 'after' not in r['query'] for r in requests))

    def test_backward_paging_covers_more_than_one_page(self):
        out = self.run_page(self.config(), self.body('Array.from({length:205},(_,i)=>message(lower+i*1000))'))
        result = self.result(out)
        messages = result['results'][0]['messages']
        self.assertEqual(len(messages), 205)
        self.assertFalse(result['results'][0]['capped'])
        self.assertEqual(len([r for r in out['calls'] if r['url'].endswith('/messages')]), 3)
        self.assertEqual([int(m['id']) for m in messages], sorted(int(m['id']) for m in messages))

    def test_archived_threads_and_dm_with_newer_activity_are_included(self):
        cfg = self.config()
        cfg.update(include_threads=True, include_archived_threads=True,
                   dm={'include': True, 'include_groups': True, 'active_within_days': 0})
        extra = """
if(url==='/users/@me/channels')return {status:200,body:[{type:1,id:'777',last_message_id:sf(upper+1000),recipients:[{username:'dm'}]}]};
if(url.endsWith('/threads/search'))return {status:200,body:{has_more:false,threads:query.archived==='true'?[{id:'888',last_message_id:sf(upper+1000),name:'archived'}]:[]}};
"""
        out = self.run_page(cfg, self.body('[message(lower+1000)]', extra))
        result = self.result(out)
        self.assertEqual({r['id'] for r in result['results']}, {'555', '777', '888'})
        self.assertEqual({r['query']['archived'] for r in out['calls'] if r['url'].endswith('/threads/search')}, {'true', 'false'})

    def test_page_error_keeps_collected_text_and_marks_source_partial(self):
        cfg = self.config()
        code = self.body('Array.from({length:101},(_,i)=>message(lower+i*1000))')
        code = code.replace("if(url.endsWith('/messages')){", "if(url.endsWith('/messages')){if(BigInt(query.before)<BigInt(sf(upper)))throw {status:403};")
        result = self.result(self.run_page(cfg, code))
        self.assertEqual(len(result['results'][0]['messages']), 100)
        self.assertTrue(result['results'][0]['capped'])
        self.assertEqual(result['skipped'][0]['why'], 'http-403')

    def test_cap_is_explicit_and_rerun_same_window_reads_again(self):
        cfg = self.config()
        cfg['max_messages_per_channel'] = 2
        out = self.run_page(cfg, self.body('[message(lower),message(lower+1000),message(lower+2000)]'), second_cfg=cfg)
        for job in out['jobs'].values():
            self.assertEqual(len(job['out']['results'][0]['messages']), 2)
            self.assertTrue(job['out']['results'][0]['capped'])

    def test_expired_session_still_fails_closed(self):
        out = self.run_page(self.config(), 'async()=>{throw {status:401};}')
        self.assertEqual(out['jobs']['a' * 32]['err'], 'authentication-expired')
        self.assertIsNone(out['jobs']['a' * 32]['out'])

    def test_archived_thread_search_follows_an_explicit_next_page(self):
        cfg = self.config()
        cfg.update(include_threads=True, include_archived_threads=True, thread_search_max_pages=1)
        extra = """
if(url.endsWith('/threads/search'))return {status:200,body:query.archived==='true'&&query.offset==='0'
?{has_more:true,threads:[{id:'888',last_message_id:sf(upper+1000),name:'first'}]}
:query.archived==='true'?{has_more:false,threads:[{id:'889',last_message_id:sf(upper+1000),name:'second'}]}
:{has_more:false,threads:[]}};
"""
        out = self.run_page(cfg, self.body('[message(lower+1000)]', extra))
        self.assertEqual({r['id'] for r in self.result(out)['results']}, {'555', '888', '889'})
        self.assertTrue(any(r.get('query', {}).get('offset') == '25' for r in out['calls']))

    def test_rich_message_content_is_preserved_without_claiming_attachment_read(self):
        body = self.body("""[Object.assign(message(lower+1000),{
attachments:[{filename:'spec.pdf',url:'https://example.test/spec.pdf'}],
embeds:[{title:'Decision',description:'Reason behind change',fields:[{name:'Impact',value:'Existing deploys'}]}],
referenced_message:{id:sf(lower-1000),timestamp:new Date(lower-1000).toISOString(),content:'Prior context',author:{username:'a'}}})]""")
        result = self.result(self.run_page(self.config(), body))
        text = df.render_harvest(result, '2026-11-03', GUILD)
        for expected in ['Reason behind change', 'Existing deploys', 'Prior context',
                         'Allegato non letto: spec.pdf', '2026-11-02T00:00:01.000Z']:
            self.assertIn(expected, text)


class TestT3WindowIngest(unittest.TestCase):
    tearDown = TestT3Ingest.tearDown

    def setUp(self):
        TestT3Ingest.setUp(self)
        window = df.parse_window('2026-08-24T00:00:00Z', '2026-08-25T00:00:00Z')
        self.intent['config']['window'] = window
        self.result['window'] = window
        df.atomic_write(df.run_path(self.intent['run_id']), json.dumps(self.intent))

    def test_bounded_ingest_never_writes_even_isolated_cursors(self):
        df.save_cursors({'555': '1200'})
        before = Path(df.CURSOR_PATH).read_bytes()
        summary, code = df.ingest_t3(self.result, self.intent)
        self.assertEqual(code, 0)
        self.assertEqual(Path(df.CURSOR_PATH).read_bytes(), before)
        self.assertFalse(summary['cursors_advanced'])
        self.assertIn('2026-08-24T09:15:00', Path(summary['harvest']).read_text())

    def test_new_run_for_same_window_keeps_all_messages_after_prior_ingest(self):
        first, _ = df.ingest_t3(self.result, self.intent)
        other = json.loads(json.dumps(self.intent))
        other['run_id'] = 'b' * 32
        df.atomic_write(df.run_path(other['run_id']), json.dumps(other))
        result = dict(self.result, run_id=other['run_id'])
        second, _ = df.ingest_t3(result, other)
        self.assertEqual((first['total'], second['total']), (1, 1))
        self.assertNotEqual(first['harvest'], second['harvest'])
        self.assertFalse(Path(df.CURSOR_PATH).exists())

    def test_bound_mismatch_or_outside_message_is_rejected(self):
        self.result['window'] = dict(self.result['window'], until_ms=0)
        with self.assertRaisesRegex(ValueError, 'window-mismatch'):
            df.ingest_t3(self.result, self.intent)
        self.result['window'] = self.intent['config']['window']
        self.result['results'][0]['messages'][0]['t'] = '2026-08-25T00:00:00Z'
        with self.assertRaisesRegex(ValueError, 'outside-window'):
            df.ingest_t3(self.result, self.intent)

    def test_ingest_rejects_default_state_even_for_failed_result(self):
        with mock.patch.object(df, 'STATE_DIR', df.DEFAULT_STATE_DIR):
            with self.assertRaisesRegex(ValueError, 'isolated-state-dir'):
                df.ingest_t3({'run_id':self.intent['run_id'],'ok':False,'why':'test'}, self.intent)


class TestPayloadAssembly(unittest.TestCase):
    def test_native_job_uses_get_client_without_extracting_credentials(self):
        expression = df.t3_start_expression("a" * 32, TestT3PageJob.config(self))
        self.assertIn("http.get(", expression)
        self.assertNotIn("Authorization", expression)
        self.assertNotIn("tokenRequest", expression)
        self.assertNotIn("fetch(", expression)

    def test_job_config_is_valid_json_inside_the_job(self):
        cfg = TestT3PageJob.config(self)
        cfg["dm"] = {"include": True, "include_groups": True, "active_within_days": 7}
        expression = df.t3_start_expression("a" * 32, cfg)
        self.assertNotIn("__CFG__", expression)
        self.assertIn('"include_groups": true', expression)

    def test_cursor_job_enumerates_active_threads_without_archived_search(self):
        self.assertIn("threads/search", df.JS_T3_CURSOR_JOB)
        self.assertIn("archived=false", df.JS_T3_CURSOR_JOB)
        self.assertNotIn("archived=true", df.JS_T3_CURSOR_JOB)

    def test_thread_enumeration_deadline_is_explicit(self):
        self.assertIn("enumDeadline", df.JS_T3_CURSOR_JOB)
        self.assertIn("deadline-enumerazione", df.JS_T3_CURSOR_JOB)

    def test_legacy_browser_commands_are_not_accepted(self):
        for command in ("doctor", "channels", "fetch"):
            with self.subTest(command=command):
                process = subprocess.run([sys.executable, SCRIPT, command],
                                         text=True, capture_output=True)
                self.assertEqual(process.returncode, 2)
                self.assertIn("invalid choice", process.stderr)


class TestConfig(unittest.TestCase):
    def test_configured_perimeter_is_loadable_and_keyed_by_id(self):
        cfg = df.load_config()
        self.assertEqual(cfg["guild"]["id"], GUILD)
        self.assertEqual([c["id"] for c in cfg["channels"]], ["555"])
        self.assertTrue(cfg["include_threads"])

    def test_duplicate_channel_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = json.loads(json.dumps(PERIMETER))
            config["channels"].append(dict(config["channels"][0]))
            path = Path(tmp, "perimeter.json")
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "duplicate-channel-id"):
                df.load_config(str(path))

    def test_channel_names_cannot_replace_stable_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = json.loads(json.dumps(PERIMETER))
            config["channels"][0]["id"] = "channel-name"
            path = Path(tmp, "perimeter.json")
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "numeric-id"):
                df.load_config(str(path))

    def test_missing_environment_stops_before_writing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp, "state")
            env = dict(os.environ)
            env.pop("SKILLS_CONFIG_DIR", None)
            process = subprocess.run([sys.executable, SCRIPT, "--state-dir", str(state),
                                      "prepare-t3", "--json"], env=env,
                                     text=True, capture_output=True)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("SKILLS_CONFIG_DIR", process.stdout + process.stderr)
            self.assertFalse(state.exists())

    def test_missing_config_stops_before_writing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp, "state")
            env = dict(os.environ, SKILLS_CONFIG_DIR=str(Path(tmp, "absent")))
            process = subprocess.run([sys.executable, SCRIPT, "--state-dir", str(state),
                                      "prepare-t3", "--json"], env=env,
                                     text=True, capture_output=True)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("config", process.stdout.lower() + process.stderr.lower())
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

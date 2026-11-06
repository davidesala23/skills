#!/usr/bin/env python3
"""Prepare and commit native GET-only Discord collection jobs.

Configuration comes from SKILLS_CONFIG_DIR/ingest/config.json. Page expressions
run in the host's authenticated Discord browser; no credentials are extracted.
Harvests are saved before cursors, under a shared lock. Ingestion is idempotent
per run and cursor updates are monotonic. A bounded-window collection requires
an isolated state directory and never advances cursors. Dry runs preserve state.
"""

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid

from ingest_config import load_config as load_ingest_config


CONFIG_PATH = None
DEFAULT_STATE_DIR = None
STATE_DIR = None
CURSOR_PATH = None
HARVEST_DIR = None
HEALTH_PATH = None
RETENTION_DAYS = None
DEFAULT_DEADLINE = None


def configure_runtime(config, state_dir=None):
    global CONFIG_PATH, DEFAULT_STATE_DIR, STATE_DIR, CURSOR_PATH
    global HARVEST_DIR, HEALTH_PATH, RETENTION_DAYS, DEFAULT_DEADLINE
    if "discord" not in config["enabled_sources"]:
        raise ValueError("discord-source-not-enabled")
    CONFIG_PATH = config["discord_config_path"]
    DEFAULT_STATE_DIR = os.path.abspath(os.path.expanduser(config["discord_runtime_dir"]))
    STATE_DIR = os.path.abspath(os.path.expanduser(state_dir)) if state_dir else DEFAULT_STATE_DIR
    CURSOR_PATH = os.path.join(STATE_DIR, "discord-cursors.json")
    HARVEST_DIR = os.path.join(STATE_DIR, "discord")
    HEALTH_PATH = os.path.join(STATE_DIR, "discord-health.json")
    RETENTION_DAYS = config["discord_retention_days"]
    DEFAULT_DEADLINE = config["discord_deadline_seconds"]


def load_config(path=None):
    path = path or CONFIG_PATH
    if path is None:
        raise ValueError("discord-runtime-not-configured")
    with open(path, encoding="utf-8") as source:
        cfg = json.load(source)
    if not isinstance(cfg, dict):
        raise ValueError("discord-perimeter-must-be-object")
    if not isinstance(cfg.get("guild"), dict) or not isinstance(cfg["guild"].get("id"), str) or not cfg["guild"]["id"].isdigit():
        raise ValueError("discord-perimeter-needs-guild-id")
    if not isinstance(cfg.get("expected_user_id"), str) or not cfg["expected_user_id"].isdigit():
        raise ValueError("discord-perimeter-needs-expected-user-id")
    if not isinstance(cfg.get("channels"), list):
        raise ValueError("discord-perimeter-needs-channels")
    ids = set()
    for channel in cfg["channels"]:
        if not isinstance(channel, dict) or not isinstance(channel.get("id"), str) or not channel["id"].isdigit():
            raise ValueError("discord-channel-needs-numeric-id")
        if channel["id"] in ids:
            raise ValueError("discord-duplicate-channel-id")
        ids.add(channel["id"])
        if not isinstance(channel.get("name"), str) or not channel["name"]:
            raise ValueError("discord-channel-needs-name")
        if not isinstance(channel.get("categoria", ""), str):
            raise ValueError("discord-channel-invalid-category")
        if not isinstance(channel.get("attivo", True), bool):
            raise ValueError("discord-channel-invalid-enabled-flag")
        channel.setdefault("categoria", "")
    if not isinstance(cfg.get("dm"), dict) or not isinstance(cfg["dm"].get("include"), bool):
        raise ValueError("discord-perimeter-needs-dm-setting")
    if cfg["dm"]["include"]:
        if not isinstance(cfg["dm"].get("include_groups"), bool):
            raise ValueError("discord-perimeter-needs-dm-group-setting")
        days = cfg["dm"].get("active_within_days")
        if type(days) is not int or days < 0:
            raise ValueError("discord-perimeter-needs-dm-lookback")
    for key in ("seed_days", "max_messages_per_channel"):
        if type(cfg.get(key)) is not int or cfg[key] <= 0:
            raise ValueError("discord-perimeter-needs-positive-" + key)
    if not isinstance(cfg.get("include_threads"), bool):
        raise ValueError("discord-perimeter-needs-thread-setting")
    return cfg


def load_cursors():
    try:
        with open(CURSOR_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cursors(cursors):
    atomic_write(CURSOR_PATH, json.dumps(cursors, indent=1, sort_keys=True))


def load_health():
    try:
        with open(HEALTH_PATH) as f:
            h = json.load(f)
        return {"consecutive_down": int(h.get("consecutive_down", 0)),
                "down_since": h.get("down_since"),
                "last_marked": h.get("last_marked"),
                "last_ok": h.get("last_ok")}
    except (OSError, ValueError, TypeError):
        # Also the corrupt case. A health file we cannot read must read as HEALTHY,
        # never as a long outage: a fabricated "giu' da 40 giorni" is actionable and
        # wrong, which is worse than no number at all.
        return {"consecutive_down": 0, "down_since": None,
                "last_marked": None, "last_ok": None}


def save_health(h):
    atomic_write(HEALTH_PATH, json.dumps(h, indent=1, sort_keys=True))


def note_down(today):
    """A down run bumps the streak, but only ONCE per day.

    The reported count is *consecutive days down*, not *consecutive failed
    invocations*: collection can run twice in a morning, and a re-run must not
    make the outage look twice as old. `down_since` is the first day of the current
    outage and is what makes the counter re-derivable if this file is lost.
    """
    h = load_health()
    if h.get("down_since") is None:
        h["down_since"] = today
        h["consecutive_down"] = 1
    elif h.get("last_marked") != today:
        h["consecutive_down"] = int(h.get("consecutive_down", 0)) + 1
    h["last_marked"] = today
    save_health(h)
    return h


def note_up(today):
    """A single success clears the outage. No decay, no grace period: the channel
    either answered this run or it did not."""
    save_health({"consecutive_down": 0, "down_since": None,
                 "last_marked": None, "last_ok": today})


def permalink(guild_id, block, message_id):
    # Only DMs and group DMs live under @me. Everything else — a guild channel, and a
    # thread, which is a channel too — is scoped by the guild. Stated this way round on
    # purpose: an unknown future kind must degrade to a link that resolves, not to @me.
    scope = "@me" if block["kind"] in ("dm", "group") else guild_id
    return f"https://discord.com/channels/{scope}/{block['id']}/{message_id}"


def render_harvest(res, when, guild_id):
    """Markdown harvest — what the content reader reads. Bodies verbatim."""
    lines = [f"# Discord harvest — {when}", ""]
    window = res.get('window')
    if window:
        lines += [f"Finestra: {window['since']} incluso — {window['until']} escluso", ""]
    order = {"dm": 0, "group": 1, "channel": 2, "thread": 3}
    for b in sorted(res["results"], key=lambda r: (order.get(r["kind"], 3), r["name"])):
        if not b["messages"]:
            continue
        label = {"dm": "DM", "group": "GRUPPO", "channel": "CANALE",
                 "thread": "THREAD"}[b["kind"]]
        head = f"## {label} — {b['name']}"
        if b["cat"]:
            head += f"  ({b['cat']})"
        head += f"  · {len(b['messages'])} nuovi"
        if b["capped"]:
            head += ("  · PARZIALE: riprendere la stessa finestra" if window else
                     "  · TRONCATO al cap, il resto arriva al prossimo run")
        lines += [head, ""]
        for m in b["messages"]:
            flags = []
            if m["bot"]:
                flags.append("bot")
            if m["men"]:
                flags.append("TI MENZIONA")
            if m["ref"]:
                flags.append(f"risponde a {m['ref']}")
            if m["att"]:
                flags.append("allegati: " + ", ".join(m["att"]))
            if m["emb"]:
                flags.append(f"{m['emb']} embed")
            tag = f"  [{'; '.join(flags)}]" if flags else ""
            body = (m["c"] or "").replace("\r", "")
            first, *rest = body.split("\n") if body else [""]
            stamp = m['t'] if window else m['t'][11:16]
            lines.append(f"- **{m['a']}** {stamp}{tag}: {first}")
            lines += [f"  {extra}" for extra in rest]
            details = m.get('details', {})
            if details.get('edited_at'):
                lines.append(f"  Modificato: {details['edited_at']}")
            for attachment in details.get('attachments', []):
                lines.append(f"  Allegato non letto: {attachment.get('name', '')} <{attachment.get('url', '')}>")
            for embed in details.get('embeds', []):
                for text in [embed.get('title'), embed.get('description'), embed.get('url')]:
                    if text:
                        lines += ['  Embed: ' + line for line in text.splitlines()]
                for field in embed.get('fields', []):
                    lines.append(f"  Embed: {field.get('name', '')}: {field.get('value', '')}")
            reference = details.get('reference')
            if reference:
                lines.append(f"  Messaggio citato (contesto, {reference.get('timestamp') or 'data ignota'}):")
                lines += ['  > ' + line for line in reference.get('content', '').splitlines()]
            lines.append(f"  <{permalink(guild_id, b, m['id'])}>")
        lines.append("")
    if res.get("skipped"):
        lines += ["## Non letti", ""]
        lines += [f"- {s['name']} — {s['why']}" for s in res["skipped"]]
        lines.append("")
    return "\n".join(lines)


def prune_harvests():
    if not os.path.isdir(HARVEST_DIR):
        return 0
    cutoff = dt.date.today() - dt.timedelta(days=RETENTION_DAYS)
    removed = 0
    for name in os.listdir(HARVEST_DIR):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:-[0-9a-f]{32})?\.md$", name)
        if not m:
            continue
        try:
            when = dt.date.fromisoformat(m.group(1))
        except ValueError:

            # after the harvest is written, so raising here would fail a run whose work
            # already succeeded, and the caller would read the channel as down.
            continue
        if when < cutoff:
            os.remove(os.path.join(HARVEST_DIR, name))
            removed += 1
    return removed


def prune_t3_runs():
    cutoff = dt.date.today() - dt.timedelta(days=RETENTION_DAYS)
    runs = os.path.join(STATE_DIR, "t3-runs")
    if os.path.isdir(runs):
        for name in os.listdir(runs):
            if not re.fullmatch(r"[0-9a-f]{32}\.json", name):
                continue
            path = os.path.join(runs, name)
            try:
                with open(path) as f:
                    when = dt.date.fromisoformat(json.load(f)["date"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if when < cutoff:
                os.remove(path)


JS_T3_API = r"""
  const chunks = window.webpackChunkdiscord_app;
  if (location.origin !== 'https://discord.com' || !chunks) throw Error('discord-client-unavailable');
  const contexts = [];
  chunks.push([[Symbol('ingest-discord-read')], {}, r => contexts.push(r)]);
  chunks.pop();
  const req = contexts.find(r => r.m && r.m[636537]);
  const http = req && req(636537).Bo;
  if (!http || typeof http.get !== 'function') throw Error('discord-native-client-changed');
  const api = async p => {
    let lastError = 'client-error';
    for (let attempt = 0; attempt < 5; attempt++) {
      const left = deadline - Date.now();
      if (left <= 0) return {__err:'deadline'};
      const [url, query] = p.split('?');
      let timer;
      try {
        const r = await Promise.race([
          http.get({url, query:Object.fromEntries(new URLSearchParams(query || ''))}),
          new Promise((_, reject) => {timer = setTimeout(() => reject(Error('request-deadline')), left);})
        ]);
        if (r.status === 401) throw Object.assign(Error('authentication-expired'), {auth:true});
        if (r.status === 429) throw r;
        if (r.status >= 400) return {__err:r.status};
        return r.body;
      } catch (e) {
        if (e.auth || e.status === 401) throw Error('authentication-expired');
        lastError = e.status || e.message || 'client-error';
        if (e.status && e.status !== 429) return {__err:lastError};
        const wait = e.status === 429 ? (e.body?.retry_after || 1) * 1000 + 300 : Math.min(800 * 2 ** attempt, 5000);
        if (Date.now() + wait >= deadline) return {__err:'deadline'};
        await sleep(wait);
      } finally { clearTimeout(timer); }
    }
    return {__err:lastError};
  };
"""


JS_T3_WINDOW_JOB = r"""
const jobs = window.__ingestDiscordJobs ||= {};
if (jobs[RUN_ID]) return {run_id:RUN_ID, started:false};
const job = jobs[RUN_ID] = {done:false, err:null, out:null, prog:0, of:0};
(async () => {
 try {
  const CFG = __CFG__;
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const deadline = Date.now() + CFG.deadline_seconds * 1000;
  __T3_API__
  const W = CFG.window;
  const ts = sf => Number((BigInt(sf) >> 22n) + 1420070400000n);
  const sfOf = ms => (BigInt(ms) - 1420070400000n) << 22n;
  const me = await api('/users/@me');
  if (!me || me.__err || me.id !== CFG.expected_user_id) throw Error('discord-account-mismatch-or-unavailable');
  const ME = me.id;
  const guilds = await api('/users/@me/guilds');
  if (!Array.isArray(guilds) || !guilds.some(g=>g.id===CFG.guild_id)) throw Error('configured-guild-unavailable');
  const work = [], seen = new Set(), skipped = [];
  const add = w => {if(!seen.has(w.id)){seen.add(w.id);work.push(w);}};
  for(const c of CFG.channels) add({kind:'channel',id:c.id,name:c.name,cat:c.categoria||''});
  if(CFG.dm.include){
    const dms = await api('/users/@me/channels');
    if(!Array.isArray(dms)) skipped.push({name:'(elenco DM)',why:'http-'+(dms?.__err||'invalid-response')});
    else for(const c of dms){
      if(![1,3].includes(c.type) || !c.last_message_id || ts(c.last_message_id)<W.since_ms) continue;
      if(c.type===3 && !CFG.dm.include_groups) continue;
      add({kind:c.type===3?'group':'dm',id:c.id,
           name:(c.recipients||[]).map(r=>r.global_name||r.username).join(', ')||c.name||'(dm)',cat:''});
    }
  }
  if(CFG.include_threads){
    const enumDeadline = Date.now() + Math.max(0,deadline-Date.now())/3;
    for(const c of CFG.channels){
      for(const archived of CFG.include_archived_threads?[false,true]:[false]){
        const label='thread '+(archived?'archiviati':'attivi')+' di '+c.name;
        let offset=0;
        while(true){
          if(Date.now()>enumDeadline){skipped.push({name:label,why:'deadline-enumerazione'});break;}
          const s=await api('/channels/'+c.id+'/threads/search?sort_by=last_message_time&sort_order=desc&archived='+archived+'&limit=25&offset='+offset);
          if(!s || s.__err || !Array.isArray(s.threads)){skipped.push({name:label,why:'http-'+(s?.__err||'invalid-response')});break;}
          for(const t of s.threads){
            if(t.last_message_id && ts(t.last_message_id)>=W.since_ms)
              add({kind:'thread',id:t.id,name:c.name+' \u203a '+(t.name||'(senza titolo)'),cat:c.categoria||''});
          }
          if(s.has_more===false || (s.has_more!==true && s.threads.length<25)) break;
          if(!s.threads.length){skipped.push({name:label,why:'empty-thread-page-with-more'});break;}
          offset+=25;
          await sleep(150);
        }
      }
    }
  }
  job.of=work.length;
  const results=[];
  let deadlineHit=false;
  for(const w of work){
    if(Date.now()>deadline){deadlineHit=true;skipped.push({name:w.name,why:'deadline'});continue;}
    const messages=new Map();
    let before=sfOf(W.until_ms), capped=false, error=null;
    while(true){
      if(Date.now()>deadline){deadlineHit=true;capped=true;error='deadline';break;}
      const batch=await api('/channels/'+w.id+'/messages?limit=100&before='+before);
      if(!Array.isArray(batch)){
        if(batch?.__err==='deadline') deadlineHit=true;
        error='http-'+(batch?.__err||'invalid-response');break;
      }
      if(!batch.length) break;
      batch.sort((a,b)=>BigInt(a.id)>BigInt(b.id)?-1:1);
      const oldest=BigInt(batch[batch.length-1].id);
      if(oldest>=before){error='message-pagination-not-progressing';break;}
      for(const m of batch){
        const at=Date.parse(m.timestamp);
        if(!Number.isFinite(at)){error='invalid-message-timestamp';break;}
        if(at<W.since_ms || at>=W.until_ms || messages.has(m.id)) continue;
        if(messages.size>=CFG.max_messages_per_channel){capped=true;break;}
        messages.set(m.id,m);
      }
      if(error || capped) break;
      const complete=batch.length<100 || oldest<=sfOf(W.since_ms);
      if(complete) break;
      if(messages.size>=CFG.max_messages_per_channel){capped=true;break;}
      before=oldest;
      await sleep(140);
    }
    if(error) skipped.push({name:w.name,why:error});
    const ordered=[...messages.values()].sort((a,b)=>BigInt(a.id)<BigInt(b.id)?-1:1);
    results.push({kind:w.kind,id:w.id,name:w.name,cat:w.cat,capped:capped||!!error,
      cursor:ordered.length?ordered[ordered.length-1].id:null,
      messages:ordered.map(m=>({id:m.id,t:m.timestamp,
        a:(m.author&&(m.author.global_name||m.author.username))||'?',bot:!!m.author?.bot,c:m.content||'',
        att:(m.attachments||[]).map(a=>a.filename),emb:(m.embeds||[]).length,
        ref:m.referenced_message?((m.referenced_message.author||{}).username||''):null,
        men:(m.mentions||[]).some(u=>u.id===ME),
        details:{edited_at:m.edited_timestamp||null,
          attachments:(m.attachments||[]).map(a=>({name:a.filename,url:a.url,content_type:a.content_type||null})),
          embeds:(m.embeds||[]).map(e=>({title:e.title||'',description:e.description||'',url:e.url||'',fields:(e.fields||[]).map(f=>({name:f.name,value:f.value}))})),
          reference:m.referenced_message?{id:m.referenced_message.id,content:m.referenced_message.content||'',timestamp:m.referenced_message.timestamp||null}:null}
      }))});
    job.prog++;
    await sleep(120);
  }
  job.out={run_id:RUN_ID,ok:true,user_id:ME,guild_id:CFG.guild_id,window:W,results,skipped,deadlineHit};
 }catch(e){job.err=String(e?.message||e);}
 job.done=true;
})();
return {run_id:RUN_ID,started:true};
"""


def t3_expressions(run_id, cfg):
    key = json.dumps(run_id)
    value = ('(() => {const job=window.__ingestDiscordJobs?.[' + key + '];'
             'if(!job)return {run_id:' + key + ',ok:false,why:"page-job-missing"};'
             'if(!job.done)return {run_id:' + key + ',ok:false,why:"page-job-not-finished"};'
             'return job.err?{run_id:' + key + ',ok:false,why:job.err}:job.out;})()')
    return {
        'run_id': run_id,
        'start_expression': t3_start_expression(run_id, cfg),
        'poll_expression': ('(() => {const j=window.__ingestDiscordJobs?.[' + key + '];'
                            'return j?{done:j.done,error:j.err,progress:j.prog,total:j.of}:'
                            '{done:true,error:"page-job-missing"};})()'),
        'read_expression': value,
        'read_chunk_expression': 'JSON.stringify(' + value + ').slice(__OFFSET__, __OFFSET__ + __LENGTH__)',
        'cleanup_expression': 'delete window.__ingestDiscordJobs?.[' + key + ']',
    }


def atomic_write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.discord-', dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def state_lock():
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, 'discord.lock'), 'a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def run_path(run_id):
    if not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise ValueError('invalid-run-id')
    return os.path.join(STATE_DIR, 't3-runs', run_id + '.json')


def parse_window(since, until):
    if not since and not until:
        return None
    if not since or not until:
        raise ValueError('since-and-until-required-together')
    times = []
    for value in (since, until):
        if not isinstance(value, str) or not re.fullmatch(
                r'\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:[Zz]|[+-]\d{2}:\d{2})', value):
            raise ValueError('window-requires-RFC3339-with-timezone-and-millisecond-precision')
        parsed = dt.datetime.fromisoformat(value.upper().replace('Z', '+00:00')).astimezone(dt.timezone.utc)
        if parsed.timestamp() < 1420070400:
            raise ValueError('window-predates-discord-epoch')
        times.append(parsed)
    if times[0] >= times[1]:
        raise ValueError('since-must-precede-until')
    return {'since': times[0].isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            'until': times[1].isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            'since_ms': round(times[0].timestamp() * 1000),
            'until_ms': round(times[1].timestamp() * 1000)}


def require_isolated_window_state():
    if os.path.realpath(STATE_DIR) == os.path.realpath(DEFAULT_STATE_DIR):
        raise ValueError('bounded-window-requires-isolated-state-dir')


def make_job_config(cfg, deadline_seconds, window=None, include_archived_threads=False):
    if deadline_seconds <= 0:
        raise ValueError('deadline must be positive')
    return {
        'channels': [c for c in cfg['channels'] if c.get('attivo', True)],
        'dm': cfg['dm'], 'cursors': {} if window else load_cursors(),
        'seed_days': cfg['seed_days'],
        'include_threads': cfg['include_threads'],
        'max_messages_per_channel': cfg['max_messages_per_channel'],
        'deadline_seconds': deadline_seconds,
        'expected_user_id': cfg.get('expected_user_id'), 'guild_id': cfg['guild']['id'],
        **({'window': window, 'include_archived_threads': include_archived_threads} if window else {}),
    }


def cmd_prepare_t3(args):
    window = parse_window(args.since, args.until)
    if window:
        if not args.state_dir:
            raise ValueError('bounded-window-requires-explicit-isolated-state-dir')
        require_isolated_window_state()
    elif args.include_archived_threads:
        raise ValueError('archived-thread-option-requires-bounded-window')
    cfg = make_job_config(load_config(args.config), args.deadline_seconds,
                          window, args.include_archived_threads)
    if window and (type(cfg['max_messages_per_channel']) is not int or cfg['max_messages_per_channel'] <= 0):
        raise ValueError('max-messages-per-channel-must-be-positive-integer')
    if not isinstance(cfg['expected_user_id'], str) or not cfg['expected_user_id'].isdigit():
        raise ValueError('configure expected_user_id after verifying the T3 account')
    run_id = uuid.uuid4().hex
    intent = {'run_id': run_id, 'date': dt.date.today().isoformat(),
              'dry_run': args.dry_run, 'config': cfg}
    atomic_write(run_path(run_id), json.dumps(intent, ensure_ascii=False))
    print(json.dumps(t3_expressions(run_id, cfg), ensure_ascii=False))
    return 0


def validate_t3_result(res, intent):
    if not isinstance(res, dict) or res.get('run_id') != intent['run_id']:
        raise ValueError('result-run-mismatch')
    if res.get('ok') is not True:
        why = res.get('why')
        if not isinstance(why, str) or not why:
            raise ValueError('missing-failure-reason')
        return
    cfg = intent['config']
    window = cfg.get('window')
    if window:
        require_isolated_window_state()
        if res.get('window') != window:
            raise ValueError('result-window-mismatch')
    if res.get('user_id') != cfg['expected_user_id'] or res.get('guild_id') != cfg['guild_id']:
        raise ValueError('result-identity-mismatch')
    if not isinstance(res.get('results'), list) or not isinstance(res.get('skipped'), list) or not isinstance(res.get('deadlineHit'), bool):
        raise ValueError('invalid-result-shape')
    seen = set()
    for b in res['results']:
        if not isinstance(b, dict) or b.get('kind') not in ('channel', 'dm', 'group', 'thread'):
            raise ValueError('invalid-source')
        if not isinstance(b.get('id'), str) or not b['id'].isdigit() or b['id'] in seen:
            raise ValueError('invalid-or-duplicate-source-id')
        seen.add(b['id'])
        if not isinstance(b.get('messages'), list) or not isinstance(b.get('capped'), bool):
            raise ValueError('invalid-source-shape')
        if not isinstance(b.get('name'), str) or not isinstance(b.get('cat'), str):
            raise ValueError('invalid-source-label')
        ids = []
        for m in b['messages']:
            if not isinstance(m, dict):
                raise ValueError('invalid-message-shape')
            if not isinstance(m.get('id'), str) or not m['id'].isdigit():
                raise ValueError('invalid-message-id')
            ids.append(int(m['id']))
            if not all(isinstance(m.get(k), str) for k in ('t', 'a', 'c')) or not isinstance(m.get('att'), list):
                raise ValueError('invalid-message-shape')
            if not all(isinstance(a, str) for a in m['att']) or not (m.get('ref') is None or isinstance(m['ref'], str)):
                raise ValueError('invalid-message-attachments-or-reference')
            if not isinstance(m.get('bot'), bool) or not isinstance(m.get('men'), bool) or not isinstance(m.get('emb'), int):
                raise ValueError('invalid-message-flags')
            if window:
                try:
                    message_time = dt.datetime.fromisoformat(m['t'].replace('Z', '+00:00'))
                    lower = dt.datetime.fromisoformat(window['since'].replace('Z', '+00:00'))
                    upper = dt.datetime.fromisoformat(window['until'].replace('Z', '+00:00'))
                    inside = lower <= message_time < upper
                except (ValueError, TypeError):
                    inside = False
                if not inside:
                    raise ValueError('message-outside-window')
        if ids and (ids != sorted(set(ids)) or b.get('cursor') != str(max(ids))):
            raise ValueError('invalid-cursor-or-message-order')
    for skipped in res['skipped']:
        if not isinstance(skipped, dict):
            raise ValueError('invalid-skip')
        if not isinstance(skipped.get('name'), str) or not isinstance(skipped.get('why'), str):
            raise ValueError('invalid-skip')
    skipped_names = {s['name'] for s in res['skipped']}
    for channel in cfg['channels']:
        if channel['id'] not in seen and channel['name'] not in skipped_names:
            raise ValueError('configured-channel-missing')


def ingest_t3(res, intent, dry_run=False):
    if intent['config'].get('window'):
        require_isolated_window_state()
    validate_t3_result(res, intent)
    dry_run = dry_run or intent['dry_run']
    today = intent['date']
    if not res['ok']:
        if dry_run:
            health = load_health()
        else:
            with state_lock():
                health = note_down(today)
        return {'ok': False, 'channel': 'discord', 'method': 't3-native-discord-client-get', 'run_id': intent['run_id'], 'status': 'down', 'why': res['why'],
                **health, 'dry_run': dry_run}, 1
    total = sum(len(b['messages']) for b in res['results'])
    partial = bool(res['skipped'] or res['deadlineHit'] or any(b['capped'] for b in res['results']))
    harvest = os.path.join(HARVEST_DIR, today + '-' + intent['run_id'] + '.md')
    summary = {'ok': True, 'channel': 'discord', 'method': 't3-native-discord-client-get', 'run_id': intent['run_id'], 'status': 'partial' if partial else 'up',
               'date': today, 'total': total, 'harvest': harvest if total else None,
               'sources': [{'kind': b['kind'], 'name': b['name'], 'n': len(b['messages']), 'capped': b['capped']}
                           for b in res['results'] if b['messages']],
               'skipped': res['skipped'], 'deadline_hit': res['deadlineHit'], 'dry_run': dry_run}
    if intent['config'].get('window'):
        summary['window'] = intent['config']['window']
        summary['cursors_advanced'] = False
    if dry_run:
        summary['harvest'] = None
        return summary, 0
    with state_lock():
        path = run_path(intent['run_id'])
        with open(path) as f:
            saved = json.load(f)
        digest = hashlib.sha256(json.dumps(res, sort_keys=True).encode()).hexdigest()
        if saved.get('result_sha256') not in (None, digest):
            raise ValueError('run-result-changed')
        if saved.get('receipt'):
            return saved['receipt'], 0
        saved['result_sha256'] = digest
        atomic_write(path, json.dumps(saved, ensure_ascii=False))
        if total:
            atomic_write(harvest, render_harvest(res, today, intent['config']['guild_id']))
            if not intent['config'].get('window'):
                cursors = load_cursors()
                for b in res['results']:
                    if b['messages']:
                        cursors[b['id']] = str(max(int(cursors.get(b['id'], '0')), int(b['cursor'])))
                atomic_write(CURSOR_PATH, json.dumps(cursors, sort_keys=True, indent=1))
        # A partial read is visible as partial; only complete success clears an outage.
        if not partial:
            note_up(today)
        saved['receipt'] = summary
        atomic_write(path, json.dumps(saved, ensure_ascii=False))
        prune_harvests()
        prune_t3_runs()
    return summary, 0


def cmd_ingest_t3(args):
    with open(run_path(args.run_id)) as f:
        intent = json.load(f)
    source = sys.stdin if args.input == '-' else open(args.input)
    try:
        res = json.load(source)
    finally:
        if source is not sys.stdin:
            source.close()
    summary, code = ingest_t3(res, intent, args.dry_run)
    print(json.dumps(summary, ensure_ascii=False))
    return code


JS_T3_CURSOR_JOB = r"""

const jobs = window.__ingestDiscordJobs ||= {};
if (jobs[RUN_ID]) return {run_id:RUN_ID, started:false};
const job = jobs[RUN_ID] = {done:false, err:null, out:null, prog:0, of:0};
(async () => {
 try {
  const CFG = __CFG__;
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const deadline = Date.now() + CFG.deadline_seconds * 1000;
__T3_API__  const ts = sf => Number((BigInt(sf) >> 22n) + 1420070400000n);
  const sfOf = ms => String((BigInt(Math.floor(ms)) - 1420070400000n) << 22n);
  const now = Date.now(), DAY = 86400000;
  const meRes = await api('/users/@me');

  if (!meRes || meRes.__err || meRes.id !== CFG.expected_user_id) throw Error('discord-account-mismatch-or-unavailable');
  const ME = meRes.id;
  const guilds = await api('/users/@me/guilds');
  if (!Array.isArray(guilds) || !guilds.some(g => g.id === CFG.guild_id)) throw Error('configured-guild-unavailable');

  const work = CFG.channels.map(c => ({kind:'channel', id:c.id, name:c.name, cat:c.categoria}));
  const skipped = [];
  if (CFG.dm.include) {
    const dms = await api('/users/@me/channels');
    if (dms.__err) {
      skipped.push({name:'(elenco DM)', why:'http-'+dms.__err});
    } else {
      for (const c of dms) {
        if (!c.last_message_id) continue;
        if (c.type === 3 && !CFG.dm.include_groups) continue;
        const cur = CFG.cursors[c.id];

        const fresh = cur ? BigInt(c.last_message_id) > BigInt(cur)
                          : (now - ts(c.last_message_id)) / DAY <= CFG.dm.active_within_days;
        if (!fresh) continue;
        const who = (c.recipients||[]).map(r=>r.global_name||r.username).join(', ') || c.name || '(dm)';
        work.push({kind: c.type === 3 ? 'group' : 'dm', id:c.id, name:who, cat:''});
      }
    }
  }






  if (CFG.include_threads) {



    const enumDeadline = now + CFG.deadline_seconds * 1000 / 3;
    for (const c of CFG.channels) {
      if (Date.now() > enumDeadline) {
        skipped.push({name: 'thread di ' + c.name, why: 'deadline-enumerazione'});
        continue;
      }
      let offset = 0;
      while (true) {
        const s = await api('/channels/' + c.id + '/threads/search'
          + '?sort_by=last_message_time&sort_order=desc&archived=false'
          + '&limit=25&offset=' + offset);
        if (s.__err) { skipped.push({name: 'thread di ' + c.name, why: 'http-' + s.__err}); break; }
        const found = s.threads || [];
        for (const t of found) {
          if (!t.last_message_id) continue;
          const cur = CFG.cursors[t.id];
          const fresh = cur ? BigInt(t.last_message_id) > BigInt(cur)
                            : (now - ts(t.last_message_id)) / DAY <= CFG.seed_days;
          if (!fresh) continue;
          work.push({kind: 'thread', id: t.id,
                     name: c.name + ' \u203a ' + (t.name || '(senza titolo)'),
                     cat: c.categoria || ''});
        }

        if (s.has_more === false || (s.has_more !== true && found.length < 25)) break;
        if (!found.length) {
          skipped.push({name: 'thread di ' + c.name, why: 'empty-thread-page-with-more'}); break;
        }
        if (Date.now() > enumDeadline) {
          skipped.push({name: 'thread di ' + c.name, why: 'deadline-enumerazione'}); break;
        }
        offset += 25;
        await sleep(150);
      }
      await sleep(150);
    }
  }

  job.of = work.length;

  const results = [];
  let deadlineHit = false;
  for (const w of work) {
    if (Date.now() > deadline) { deadlineHit = true; skipped.push({name:w.name, why:'deadline'}); continue; }
    let cur = CFG.cursors[w.id] || sfOf(now - CFG.seed_days * DAY);
    const msgs = [];
    let capped = false, err = null;
    while (true) {
      const batch = await api('/channels/'+w.id+'/messages?limit=100&after='+cur);
      if (batch.__err) { err = 'http-'+batch.__err; break; }
      if (!batch.length) break;
      batch.sort((a,b) => BigInt(a.id) < BigInt(b.id) ? -1 : 1);
      for (const m of batch) msgs.push(m);
      cur = batch[batch.length-1].id;
      if (batch.length < 100) break;
      if (msgs.length >= CFG.max_messages_per_channel) { capped = true; break; }
      if (Date.now() > deadline) { deadlineHit = true; capped = true; break; }
      await sleep(140);
    }
    if (err) { skipped.push({name:w.name, why:err}); continue; }
    results.push({
      kind: w.kind, id: w.id, name: w.name, cat: w.cat, capped,
      cursor: msgs.length ? msgs[msgs.length-1].id : (CFG.cursors[w.id] || null),
      messages: msgs.map(m => ({
        id: m.id,
        t: m.timestamp,
        a: (m.author && (m.author.global_name || m.author.username)) || '?',
        bot: !!(m.author && m.author.bot),
        c: m.content || '',
        att: (m.attachments||[]).map(x=>x.filename),
        emb: (m.embeds||[]).length,
        ref: m.referenced_message ? ((m.referenced_message.author||{}).username || '') : null,
        men: (m.mentions||[]).some(u => u.id === ME)
      }))
    });
    job.prog++;
    await sleep(120);
  }
  job.out = {run_id:RUN_ID, ok:true, user_id:ME, guild_id:CFG.guild_id, results, skipped, deadlineHit};
 } catch(e) { job.err = String(e?.message || e); }
 job.done = true;
})();
return {run_id:RUN_ID, started:true};

"""


def t3_start_expression(run_id, cfg):
    template = JS_T3_WINDOW_JOB if cfg.get("window") else JS_T3_CURSOR_JOB
    body = template.replace("__T3_API__", JS_T3_API).replace("__CFG__", json.dumps(cfg))
    return "(() => {const RUN_ID = " + json.dumps(run_id) + ";\n" + body + "\n})()"



def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--state-dir", help="override runtime state directory for isolated collection")
    sub = ap.add_subparsers(dest="cmd", required=True)
    prepare = sub.add_parser("prepare-t3", help="prepare a native GET-only browser job")
    prepare.add_argument("--json", action="store_true")
    prepare.add_argument("--dry-run", action="store_true")
    prepare.add_argument("--deadline-seconds", type=int)
    prepare.add_argument("--since", help="inclusive RFC3339 start, requires --until and isolated --state-dir")
    prepare.add_argument("--until", help="exclusive RFC3339 end, requires --since")
    prepare.add_argument("--config", help="caller-specific JSON perimeter; shared config is unchanged")
    prepare.add_argument("--include-archived-threads", action="store_true")
    ingest = sub.add_parser("ingest-t3", help="validate and commit a native browser result")
    ingest.add_argument("run_id")
    ingest.add_argument("--input", default="-")
    ingest.add_argument("--json", action="store_true")
    ingest.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        configure_runtime(load_ingest_config(), args.state_dir)
        if args.cmd == "prepare-t3" and args.deadline_seconds is None:
            args.deadline_seconds = DEFAULT_DEADLINE
        return {"prepare-t3": cmd_prepare_t3, "ingest-t3": cmd_ingest_t3}[args.cmd](args)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"ok": False, "channel": "discord", "status": "down", "why": str(error)}))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

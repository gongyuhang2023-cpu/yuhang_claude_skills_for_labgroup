"""Inject a best-effort, self-contained run receipt into a Bash/Slurm script."""
import re
import os
from pathlib import Path
import subprocess
import sys

BEGIN = '# >>> spark run record >>>'
END = '# <<< spark run record <<<'

# This code travels with the uploaded script; no installed helper is required.
RUNTIME_HEADER = r'''
import datetime, hashlib, json, os, pathlib, re, shutil, signal, socket, subprocess, sys, time
from urllib.parse import urlsplit, urlunsplit
EXPECTED_COMMIT = None
DB_ROOT = '/opt/bio/db'
DB_PART = r'[A-Za-z0-9][A-Za-z0-9_.+-]*'

def command(args, timeout=5):
    r = subprocess.run(args, capture_output=True, text=True, errors='replace',
                       timeout=timeout, env=dict(os.environ, GIT_OPTIONAL_LOCKS='0',
                                                GIT_TERMINAL_PROMPT='0'))
    if r.returncode:
        raise ValueError('command failed')
    return r.stdout.strip()

def remote_url(url):
    if '://' in url:
        u = urlsplit(url)
        return urlunsplit((u.scheme, u.netloc.rsplit('@', 1)[-1], u.path, '', ''))
    return re.sub(r'^[^/@:]+@', '', url).split('?', 1)[0].split('#', 1)[0]

def version(executable, timeout):
    p = None
    try:
        deadline = time.monotonic() + timeout
        # Tools disagree (checked on Spark, 2026-11-05): mmseqs, seqkit and defense-finder want
        # `version`, blastn `-version`, prodigal `-v`, and hmmsearch only prints it inside `-h`.
        for argument in ('--version', 'version', '-version', '-v', '-V', '-h'):
            remaining = deadline - time.monotonic()
            if remaining <= 0: return 'unknown'
            p = subprocess.Popen([str(executable), argument], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, start_new_session=True)
            out, _ = p.communicate(timeout=remaining)
            if p.returncode: continue
            lines = [line.strip() for line in out.decode('utf-8', 'replace').splitlines() if line.strip()]
            if argument == '-h':
                lines = [line for line in lines[:5] if re.search(r'\d+\.\d+', line)]
            if lines: return lines[0][:1000]
    except Exception:
        if p is not None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
                p.communicate(timeout=1)
            except Exception:
                pass
    return 'unknown'

'''

# Keep a single parser for the scanner and the self-contained job receipt.
SHELL_PARSER = r'''
import bisect, pathlib, re

def shell_commands(text, with_lines=False, with_args=False):
    """Conservatively read command positions, never evaluate shell text.

    Quotes/comments and here-doc data are opaque; $(...) is scanned separately.
    With with_lines=True, return every (command, 1-based line) in source order.
    With with_args=True, also return the literal arguments before the next shell operator.
    """
    prefixes = {'env','time','srun','nohup','exec','command'}
    takes_value = {
        'env': {'-u','--unset','-C','--chdir'},
        'time': {'-f','--format','-o','--output'},
        'exec': {'-a'}, 'nohup': set(), 'command': set(),
        'srun': {'-n','--ntasks','-N','--nodes','-c','--cpus-per-task','--mem',
                 '-t','--time','-p','--partition','--gres','-D','--chdir',
                 '-o','--output','-e','--error','-J','--job-name','--export'},
    }
    no_value = {'--','-i','--ignore-environment','-0','--null','-p','-v','--verbose',
                '-a','--append','-c','-l','--exclusive','--pty','-u','--unbuffered'}
    def extract(tokens):
        result=[]; expect=True; redirect=False; option_value=False; wrapper=None
        for i,(kind,word,offset) in enumerate(tokens):
            if kind=='op':
                if word in (';', '\n', '|', '||', '&&', '&', '(', ';;', ';&'):
                    expect=True; wrapper=None; option_value=False
                elif word==')': expect=False
                elif '<' in word or '>' in word: redirect=True
                continue
            if redirect: redirect=False; continue
            if not expect: continue
            if option_value: option_value=False; continue
            if re.match(r'^[A-Za-z_][A-Za-z0-9_]*\+?=',word): continue
            if word.isdigit() and i+1<len(tokens) and tokens[i+1][0]=='op' and any(c in tokens[i+1][1] for c in '<>'):
                continue
            if wrapper and word.startswith('-'):
                if wrapper == 'command' and word in ('-v', '-V'): expect=False
                elif word in takes_value[wrapper]: option_value=True
                elif word in no_value or '=' in word or any(word.startswith(v) and len(word)>len(v) for v in takes_value[wrapper] if v.startswith('-') and not v.startswith('--')):
                    pass
                else: expect=False  # Unknown option syntax: do not guess a program.
                continue
            base=pathlib.PurePosixPath(word).name
            if base in prefixes: wrapper=base; continue
            if word in ('!', 'if', 'then', 'elif', 'else', 'while', 'until', 'do', '{', '}'):
                continue
            # A function declaration is not an invocation.
            if i+2<len(tokens) and tokens[i+1][:2]==('op','(') and tokens[i+2][:2]==('op',')'):
                expect=False; continue
            args=[]
            if with_args:
                for j in range(i+1,len(tokens)):
                    arg_kind,arg,_=tokens[j]
                    if arg_kind=='op': break
                    args.append(arg)
            result.append((word,offset,args)); expect=False
        return result
    def lex(start=0, stop=False):
        tokens=[]; nested=[]; word=''; offset=None; quote=None; depth=0; i=start
        docs=[]; doc_next=None
        def flush():
            nonlocal word,doc_next,offset
            if word:
                tokens.append(('word',word,offset))
                if doc_next is not None:
                    docs.append((word,doc_next)); doc_next=None
                word=''
            offset=None
        while i<len(text):
            c=text[i]
            if offset is None and c not in ' \t\r\n;|&()<>#' and not text.startswith('\\\n',i):
                offset=i
            if quote=="'":
                if c=="'": quote=None
                else: word+=c
                i+=1; continue
            if c=='\\' and i+1<len(text):
                if text[i+1]!='\n': word+=text[i+1]
                i+=2; continue
            if c=="'" and quote is None: quote="'"; i+=1; continue
            if c=='"': quote=None if quote=='"' else '"'; i+=1; continue
            if text.startswith('$((',i):
                # Arithmetic words are not programs. Skip to its balanced end.
                level=2; i+=3
                while i<len(text) and level:
                    if text[i]=='(': level+=1
                    elif text[i]==')': level-=1
                    i+=1
                word+='0'; continue
            if text.startswith('$(',i):
                inner,sub,i=lex(i+2,True)
                nested.extend(extract(inner)); nested.extend(sub)
                word+='x'; continue
            if quote=='"': word+=c; i+=1; continue
            if c=='#' and not word:
                end=text.find('\n',i); i=len(text) if end<0 else end; continue
            if c in ' \t\r': flush(); i+=1; continue
            if c in ';|&()<>\n':
                flush()
                if c==')' and stop and depth==0: return tokens,nested,i+1
                if c=='(': depth+=1
                elif c==')': depth=max(0,depth-1)
                op=next((s for s in ('<<<','<<-','<<','>>','&&','||',';;',';&') if text.startswith(s,i)),c)
                tokens.append(('op',op,i)); i+=len(op)
                if op in ('<<','<<-'): doc_next=op=='<<-'
                if op=='\n' and docs:
                    for delimiter,tabs in docs:
                        while i<len(text):
                            end=text.find('\n',i); end=len(text) if end<0 else end
                            line=text[i:end].rstrip('\r'); i=min(end+1,len(text))
                            if (line.lstrip('\t') if tabs else line)==delimiter: break
                    docs=[]
                continue
            word+=c; i+=1
        flush()
        return tokens,nested,i
    tokens,nested,_=lex()
    commands=extract(tokens)+nested
    if with_lines or with_args:
        newlines=[m.start() for m in re.finditer('\n',text)]
        return [(word,bisect.bisect_left(newlines,offset)+1,args) if with_args
                else (word,bisect.bisect_left(newlines,offset)+1)
                for word,offset,args in sorted(commands,key=lambda item:item[1])]
    return list(dict.fromkeys(word for word,_,_ in commands))
'''
exec(SHELL_PARSER)

RUNTIME = RUNTIME_HEADER + SHELL_PARSER + r'''
def db_identity(value):
    """'/opt/bio/db/<name>/<version>[/...]' -> (name, version); current is read with one readlink only."""
    m = re.match(re.escape(DB_ROOT) + '/(' + DB_PART + ')/(' + DB_PART + ')', value or '')
    if not m:
        return None, None
    name, version = m.group(1), m.group(2)
    if version == 'current':
        try:
            target = os.readlink(os.path.join(DB_ROOT, name, 'current')).rstrip('/')
        except OSError:
            return name, None
        prefix = DB_ROOT + '/' + name + '/'
        version = (target.split('/')[0] if not target.startswith('/') else
                   target[len(prefix):].split('/')[0] if target.startswith(prefix) else None)
    return name, version

def lock_entries():
    """The project's databases.lock.json (job directory, then the Git top level)."""
    bases = [pathlib.Path.cwd()]
    try:
        bases.append(pathlib.Path(command(['git', 'rev-parse', '--show-toplevel'])))
    except Exception:
        pass
    for base in bases:
        try:
            data = json.loads((base / 'databases.lock.json').read_text(encoding='utf-8'))
            return {(e.get('name'), e.get('version')): e for e in data.get('databases') or [] if isinstance(e, dict)}
        except (OSError, ValueError, AttributeError):
            continue
    return {}

def meta_fingerprint(name, version):
    try:
        with open(os.path.join(DB_ROOT, name, version + '.meta'), encoding='utf-8') as f:
            for line in f:
                if line.startswith('fingerprint='):
                    return line.split('=', 1)[1].strip() or None
    except OSError:
        pass
    return None

def record():
    path = pathlib.Path(sys.argv[1])
    raw = path.read_bytes()
    body = re.sub(r'(?ms)^# >>> spark run record >>>\n.*?^# <<< spark run record <<<\n',
                  '', raw.decode('utf-8', 'replace'))
    def directive(name):
        m = re.findall(r'^#SBATCH\s+--' + name + r'(?:=|\s+)([^\n]+)', body, re.M)
        return m[-1].strip().strip('\"\'') if m else None
    env = os.environ
    receipt = dict(job_id=env.get('SLURM_JOB_ID'),
                   job_name=env.get('SLURM_JOB_NAME') or directive('job-name') or path.stem,
                   started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   hostname=socket.gethostname(),
                   cpus=env.get('SLURM_CPUS_PER_TASK') or directive('cpus-per-task'),
                   mem=env.get('SLURM_MEM_PER_NODE') or env.get('SLURM_MEM_PER_CPU') or directive('mem'),
                   gres=env.get('SLURM_JOB_GRES') or env.get('SLURM_GRES') or directive('gres'),
                   script_sha256=hashlib.sha256(raw).hexdigest(),
                   commit=None, branch=None, dirty_tracked=None, untracked=None, remote=None,
                   expected_commit=EXPECTED_COMMIT, ws_head=None, ws_branch=None, dirty=None,
                   code_matches_submit=False if EXPECTED_COMMIT is not None else None,
                   databases={}, tools={})
    try:
        receipt['commit'] = command(['git', 'rev-parse', '--verify', 'HEAD'])
        try:
            receipt['branch'] = command(['git', 'symbolic-ref', '--short', '-q', 'HEAD'])
        except Exception:
            pass
        receipt['dirty_tracked'] = any(command(['git', 'diff', '--no-ext-diff',
            '--ignore-space-at-eol', '--numstat'] + extra)
            for extra in ([], ['--cached']))
        receipt['untracked'] = command(['git', 'ls-files', '--others', '--exclude-standard', '-z']).split('\0')
        receipt['untracked'] = [x for x in receipt['untracked'] if x]
        receipt['dirty'] = bool(receipt['dirty_tracked'] or receipt['untracked'])
        if EXPECTED_COMMIT is not None:
            receipt['code_matches_submit'] = receipt['commit'] == EXPECTED_COMMIT
        try:
            metadata = pathlib.Path(command(['git','rev-parse','--path-format=absolute',
                                             '--git-path','ws-snapshot.json']))
            snapshot = json.loads(metadata.read_text(encoding='utf-8'))
            for key in ('ws_head','ws_branch','dirty'):
                receipt[key] = snapshot.get(key)
        except Exception:
            pass
        try:
            receipt['remote'] = remote_url(command(['git', 'config', '--get', 'remote.origin.url']))
        except Exception:
            pass
    except Exception:
        pass
    candidates = {k: v for k, v in env.items()
                  if any(s in k.upper() for s in ('DB', 'DATA_PATH', 'DATA_DIR')) or v.startswith(DB_ROOT + '/')}
    candidates.update({p: p for p in re.findall(re.escape(DB_ROOT) + '/' + DB_PART + '/' + DB_PART, body)})
    locked = lock_entries()
    for key, value in candidates.items():
        if pathlib.Path(value).exists():
            entry = {'value': value, 'resolved': os.path.realpath(value)}
            db_name, db_version = db_identity(value)
            if db_name is None:
                db_name, db_version = db_identity(entry['resolved'])
            if db_name:
                entry.update(name=db_name, version=db_version)
                pinned = locked.get((db_name, db_version)) or {}
                if pinned.get('fingerprint'):
                    entry.update(fingerprint=pinned['fingerprint'], fingerprint_source='databases.lock.json')
                elif db_version and meta_fingerprint(db_name, db_version):
                    entry.update(fingerprint=meta_fingerprint(db_name, db_version), fingerprint_source='server')
            receipt['databases'][key] = entry
    roots = [pathlib.Path('/opt/bio').resolve()]
    if env.get('CONDA_PREFIX'): roots.append(pathlib.Path(env['CONDA_PREFIX']).resolve())
    deadline = time.monotonic() + 60
    commands = shell_commands(body)
    for word in commands:
        found = shutil.which(word)
        if found is None: continue
        executable = pathlib.Path(found).resolve()
        if not any(executable.is_relative_to(root) for root in roots): continue
        name = pathlib.PurePosixPath(word).name
        if name in receipt['tools']: continue
        remaining = deadline - time.monotonic()
        receipt['tools'][name] = {'path': str(executable),
            'version': version(executable, min(15, remaining)) if remaining > 0 else 'unknown'}
        if len(receipt['tools']) >= 12:
            break
    logs = pathlib.Path.cwd() / 'logs'
    logs.mkdir(exist_ok=True)
    name = receipt['job_name'].replace('/', '_')
    job = re.sub(r'[^\w.+-]', '_', receipt['job_id'] or 'unknown')
    (logs / (name + '-' + job + '.run.json')).write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

try:
    record()
except Exception:
    print('run_record: could not write run receipt; continuing job', file=sys.stderr)
'''


def inject(script_text, expected_commit=None):
    """Keep all SBATCH directives ahead of commands; never edit the source file."""
    if BEGIN in script_text:
        return script_text
    lines = script_text.splitlines(keepends=True)
    directives = [i for i, line in enumerate(lines) if re.match(r'^#SBATCH\s', line)]
    first_command = next((i for i, line in enumerate(lines)
                          if line.strip() and not line.lstrip().startswith('#')), len(lines))
    if directives and directives[-1] >= first_command:
        raise ValueError('SBATCH directive after a command; move directives before commands')
    index = directives[-1] + 1 if directives else (1 if lines and lines[0].startswith('#!') else 0)
    block = (BEGIN + '\n(\nif command -v python3 >/dev/null 2>&1; then\n'
             "python3 - \"$0\" <<'SPARK_RUN_RECORD_PY' || "
             "printf '%s\\n' 'run_record: python3 failed; continuing job' >&2\n" +
             RUNTIME.replace('EXPECTED_COMMIT = None', 'EXPECTED_COMMIT = ' + repr(expected_commit)) + '\nSPARK_RUN_RECORD_PY\nelse\n'
             "printf '%s\\n' 'run_record: python3 unavailable; continuing job' >&2\n"
             'fi\n) || :\n' + END + '\n')
    prefix = ''.join(lines[:index])
    if prefix and not prefix.endswith('\n'):
        prefix += '\n'
    return prefix + block + ''.join(lines[index:])


def sbatch(script_path, extra):
    """WS-native submission: anonymous script, source untouched, no SSH."""
    source = Path(script_path).expanduser().resolve(strict=True)
    stream = None
    try:
        text = inject(source.read_text(encoding='utf-8'))
        stream = os.fdopen(os.memfd_create('run-record-sbatch'), 'w+b')
        stream.write(text.encode('utf-8')); stream.flush()
        uploaded = '/proc/' + str(os.getpid()) + '/fd/' + str(stream.fileno())
    except Exception as error:
        if stream is not None: stream.close(); stream = None
        print('run_record: 注入失败，使用原脚本提交 (' + type(error).__name__ + ')', file=sys.stderr)
        uploaded = str(source)
    try:
        return subprocess.run(['sbatch', '--parsable', *extra, uploaded]).returncode
    finally:
        if stream is not None: stream.close()


def main(argv):
    if len(argv) < 2 or argv[0] != 'sbatch':
        print('Usage: run_record.py sbatch SCRIPT [sbatch options]', file=sys.stderr)
        return 2
    return sbatch(argv[1], argv[2:])


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, ValueError) as error:
        print('run_record: ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)

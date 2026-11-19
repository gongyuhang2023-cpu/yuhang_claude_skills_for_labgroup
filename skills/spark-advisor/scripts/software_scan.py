"""Scan script text for direct references to the Spark toolbox."""
import ast
from collections import defaultdict
import csv
import io
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import tokenize

from run_record import shell_commands

SERVER_TOOLS = '/opt/spark-sys'
PUBLIC_R = {'R', 'Rscript'}
CATALOG_FILE = '.spark-advisor/recipes.json'
SYSTEM_DIRS = ('/usr/bin', '/bin', '/usr/sbin', '/sbin', '/usr/local/bin', '/usr/local/sbin')
ENV_DIRS = ('miniforge3/envs', 'mambaforge/envs', 'miniconda3/envs', 'anaconda3/envs',
            '.conda/envs', 'micromamba/envs', '.local/share/mamba/envs', '.pixi/envs')
CLEANUP_MANIFEST = Path('/opt/bio/var/replaced/manifest.tsv')
TOOLBOX_ENVS = '/opt/bio/pixi/envs/'
SHELL_EXTENSIONS = {'.sh', '.bash', '.zsh', '.ksh', '.sbatch', '.slurm'}
GENERATED_DIRS = {'.git', '.pixi', '.snakemake', '.nextflow', '__pycache__', 'node_modules',
                  'site-packages', 'dist-packages', '.venv', '.tox', '.nox', '.mypy_cache', '.ipynb_checkpoints'}
TOOLBOX_PATH = re.compile(r'^/opt/bio/(?:(?:bin|compat)/([^/]+)|pixi/envs/([^/]+)/(?:[^/]+/)*([^/]+))$')
PY_ARGV = {'subprocess.' + name for name in ('run', 'Popen', 'call', 'check_call', 'check_output')}
PY_SHELL = {'os.system', 'os.popen', 'subprocess.getoutput', 'subprocess.getstatusoutput'}
PY_EXEC = {'os.' + name for name in ('execl', 'execle', 'execlp', 'execlpe', 'execv', 'execve', 'execvp', 'execvpe')}
TRIPLE_QUOTES = ("'''", '"""')
GROOVY_TOKEN = re.compile(
    r'(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)|'
    r'(?P<string>\x27{3}[\s\S]*?\x27{3}|"""[\s\S]*?"""|'
    r'''(?:'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"))|(?P<word>[A-Za-z_]\w*)|[^\s]'''
)


class CatalogError(ValueError):
    pass


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def recipe_issue(recipe):
    if not isinstance(recipe, dict):
        return '配方必须是对象。'
    for key in ('tool', 'env', 'version'):
        if not nonempty(recipe.get(key)):
            return f'{key} 必须是非空字符串。'
    for key in ('channels', 'commands', 'compat', 'notes'):
        if not isinstance(recipe.get(key), list) or not all(isinstance(x, str) for x in recipe[key]):
            return f'{key} 必须是字符串列表。'
    deps = recipe.get('dependencies')
    if not isinstance(deps, dict) or not deps:
        return 'dependencies 必须是非空依赖表。'
    for name, value in deps.items():
        if not nonempty(name):
            return 'dependencies 的包名必须是非空字符串。'
        if isinstance(value, str):
            continue
        if (not isinstance(value, dict) or set(value) - {'version', 'channel'}
                or not nonempty(value.get('version')) or ('channel' in value and not nonempty(value['channel']))):
            return f'dependencies.{name} 必须是字符串，或含非空 version 和可选非空 channel 的表。'
    if 'db' not in recipe or (recipe['db'] is not None and not isinstance(recipe['db'], str)):
        return 'db 必须是字符串或 null。'
    return None


def recipe_catalog(value):
    """Validate the catalog envelope and retain independently valid recipes."""
    if not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1:
        raise CatalogError('推荐配方目录不是 schema 1。')
    if not isinstance(value.get('recipes'), list):
        raise CatalogError('schema 1 推荐配方目录缺少 recipes 数组。')
    recipes, errors = [], []
    for index, recipe in enumerate(value['recipes']):
        if SERVER_TOOLS in json.dumps(recipe, ensure_ascii=False):
            continue
        if reason := recipe_issue(recipe):
            name = recipe.get('tool') if isinstance(recipe, dict) else None
            errors.append({'tool': name if nonempty(name) else f'recipes[{index}]', 'reason': reason})
        else:
            recipes.append(recipe)
    catalog = {'schema': 1, 'source': value.get('source'), 'recipes': recipes}
    if errors:
        catalog['recipe_errors'] = errors
    return catalog


def shell_locations(source, start=1):
    for command, line, args in shell_commands(source, with_args=True):
        yield command, start + line - 1, args


def literal_command(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def python_locations(source):
    tree = ast.parse(source)
    imports = {'subprocess': 'subprocess', 'os': 'os'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name in ('subprocess', 'os'):
                    imports[item.asname or item.name] = item.name
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module in ('subprocess', 'os'):
            for item in node.names:
                imports[item.asname or item.name] = node.module + '.' + item.name
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name):
            name = imports.get(fn.id)
        elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            name = imports.get(fn.value.id, '') + '.' + fn.attr
        else:
            continue
        kwargs = {item.arg: item.value for item in node.keywords}
        arg = node.args[0] if node.args else kwargs.get('args' if name in PY_ARGV else 'cmd')
        if name in PY_ARGV:
            # Explicit executable replaces argv[0]. Skip dynamic overrides and dynamic shell mode.
            if 'executable' in kwargs and not (isinstance(kwargs['executable'], ast.Constant)
                                               and kwargs['executable'].value is None):
                exe = kwargs['executable']
                if (command := literal_command(exe)) is not None:
                    yield from shell_locations(shlex.quote(command), exe.lineno)
                continue
            shell = kwargs.get('shell', ast.Constant(False))
            if not isinstance(shell, ast.Constant) or type(shell.value) is not bool:
                continue
            literal_args = []
            if isinstance(arg, (ast.List, ast.Tuple)) and arg.elts:
                if not shell.value:
                    for item in arg.elts[1:]:
                        if (value := literal_command(item)) is None:
                            break
                        literal_args.append(value)
                arg = arg.elts[0]
            if (command := literal_command(arg)) is not None:
                source = command if shell.value else shlex.join([command, *literal_args])
                for word, line, args in shell_locations(source, arg.lineno):
                    yield word, min(line, arg.end_lineno), args
        elif name in PY_SHELL:
            # os.system accepts command=; the other functions can use positional literals.
            arg = arg if arg is not None else kwargs.get('command')
            if (command := literal_command(arg)) is not None:
                for word, line, args in shell_locations(command, arg.lineno):
                    yield word, min(line, arg.end_lineno), args
        elif name in PY_EXEC and (command := literal_command(arg)) is not None:
            yield from shell_locations(shlex.quote(command), arg.lineno)


def snake_locations(source):
    skipped = {tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.COMMENT}
    tokens = [t for t in tokenize.generate_tokens(io.StringIO(source).readline) if t.type not in skipped]
    for name, op, value in zip(tokens, tokens[1:], tokens[2:]):
        if name.type == tokenize.NAME and name.string == 'shell' and op.string in (':', '(') and value.type == tokenize.STRING:
            # Preserve literal newlines and shell escapes. Snakemake expands placeholders at runtime.
            literal = value.string.lstrip('rRuUbBfF')
            n = 3 if literal.startswith(TRIPLE_QUOTES) else 1
            yield from shell_locations(literal[n:-n], value.start[0])


def nextflow_locations(source):
    matches = [m for m in GROOVY_TOKEN.finditer(source) if m.lastgroup != 'comment']
    depth = 0
    process_depth = None
    section = None
    for i, token in enumerate(matches):
        word = token.group()
        prev = matches[i-1].group() if i else ''
        if word == '{':
            depth += 1
            if i >= 2 and matches[i-2].group() == 'process' and matches[i-1].lastgroup == 'word':
                process_depth = depth
                section = None
        elif word == '}':
            if depth == process_depth:
                process_depth = None
            depth -= 1
        elif depth == process_depth:
            if word == ':' and prev in ('input', 'output', 'when', 'script', 'shell', 'stub', 'exec'):
                section = prev
            elif token.lastgroup == 'string':
                triple = word.startswith(TRIPLE_QUOTES)
                at_line_start = not source[source.rfind('\n', 0, token.start()) + 1:token.start()].strip()
                block_start = prev == '{' or (at_line_start and prev not in ('=', ',', '(', '+', ':'))
                if ((section in ('script', 'shell', 'stub') and prev == ':')
                        or (triple and block_start and section != 'exec')):
                    n = 3 if triple else 1
                    yield from shell_locations(word[n:-n], source.count('\n', 0, token.start()) + 1)


def script_kind(path):
    ext = path.suffix.lower()
    if ext in SHELL_EXTENSIONS:
        return 'shell'
    if ext == '.py':
        return 'python'
    if path.name == 'Snakefile' or ext == '.smk':
        return 'snake'
    if ext == '.nf':
        return 'nextflow'
    if not ext:
        with path.open('rb') as stream:
            first = stream.readline(512)
        if first.startswith(b'#!'):
            if re.search(rb'\b(?:bash|zsh|ksh|dash|sh)\b', first):
                return 'shell'
            if re.search(rb'\bpython[\d.]*\b', first):
                return 'python'
    return None


def system_commands():
    """List executable names in the Spark system directories ahead of the toolbox."""
    commands = set()
    read_any = False
    for directory in SYSTEM_DIRS:
        try:
            with os.scandir(directory) as entries:
                read_any = True
                commands.update(entry.name for entry in entries
                                if entry.is_file() and os.access(entry.path, os.X_OK))
        except FileNotFoundError:
            continue
    if not read_any:
        raise FileNotFoundError('没有可读取的 Spark 系统命令目录。')
    return sorted(commands)


def toolbox_env_name(path):
    return path[len(TOOLBOX_ENVS):].split('/', 1)[0] if path.startswith(TOOLBOX_ENVS) else ''


def is_environment(directory):
    return (directory / 'conda-meta').is_dir() or (directory / 'pyvenv.cfg').is_file()


def symlink_children(directory, errors):
    try:
        if any(p.is_symlink() for p in (*reversed(directory.parents), directory)):
            return []
        return sorted(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as e:
        errors.append({'file': str(directory), 'error': '无法读取软链所在目录：' + (e.strerror or str(e))})
        return []


def symlink_target(path, errors):
    try:
        return posixpath.normpath(posixpath.join(str(path.parent), os.readlink(path)))
    except OSError as e:
        errors.append({'file': str(path), 'error': '无法读取软链目标：' + (e.strerror or str(e))})
        return ''


def toolbox_command_link(path, dest, environment=None):
    if (dest.startswith(('/opt/bio/pixi/', '/opt/bio/bin/')) and toolbox_env_name(dest) != 'r'
            and path.name not in PUBLIC_R and posixpath.basename(dest) not in PUBLIC_R):
        return {'path': str(path), 'target': dest, 'kind': 'command', 'environment': environment}
    return None


def toolbox_environment_link(path, dest):
    if path.name != 'r' and toolbox_env_name(dest) not in ('', 'r'):
        return {'path': str(path), 'target': dest, 'kind': 'environment', 'environment': str(path)}
    return None


def bin_links(directory, errors, environment=None):
    for path in symlink_children(directory, errors):
        if path.name not in PUBLIC_R and path.is_symlink():
            if link := toolbox_command_link(path, symlink_target(path, errors), environment):
                yield link


def cleanup_manifest_links(user, errors):
    """Inspect the current original paths from this user's cleanup records."""
    try:
        with CLEANUP_MANIFEST.open(encoding='utf-8-sig', newline='') as stream:
            rows = csv.DictReader(stream, delimiter='\t')
            if not {'user', 'original'} <= set(rows.fieldnames or ()):
                raise ValueError('清单缺少 user 或 original 列')
            originals = {row['original'] for row in rows if row['user'] == user}
    except (OSError, ValueError, csv.Error) as e:
        errors.append({'file': str(CLEANUP_MANIFEST), 'error': '无法读取清理清单：' + str(e)})
        return
    for original in sorted(originals, key=lambda value: value or ''):
        if not original or not Path(original).is_absolute():
            errors.append({'file': str(CLEANUP_MANIFEST), 'error': '本人记录的 original 不是绝对路径。'})
            continue
        path = Path(os.path.abspath(original))
        try:
            if any(parent.is_symlink() for parent in reversed(path.parents)):
                continue
            if path.is_symlink():
                dest = symlink_target(path, errors)
                target_env = toolbox_env_name(dest)
                if target_env and dest == TOOLBOX_ENVS + target_env:
                    link = toolbox_environment_link(path, dest)
                elif dest.startswith((TOOLBOX_ENVS, '/opt/bio/bin/')):
                    env = path.parent.parent
                    environment = str(env) if path.parent.name == 'bin' and is_environment(env) else None
                    link = toolbox_command_link(path, dest, environment)
                else:
                    link = None
                if link:
                    yield dict(link, source='cleanup-manifest')
            elif path.is_dir() and path.name != 'r':
                for link in bin_links(path / 'bin', errors, str(path)):
                    yield dict(link, source='cleanup-manifest')
        except (OSError, ValueError) as e:
            errors.append({'file': str(path), 'error': '无法核对清理清单中的路径：' + str(e)})


def toolbox_symlinks(home, user):
    """Collect current toolbox links from known home directories and the cleanup manifest."""
    links, errors = [], []
    for root in ENV_DIRS:
        for env in symlink_children(home / root, errors):
            if env.name == 'r':
                continue
            if env.is_symlink():
                if link := toolbox_environment_link(env, symlink_target(env, errors)):
                    links.append(link)
            elif env.is_dir():
                links.extend(bin_links(env / 'bin', errors, str(env)))
    for root in ('.local/bin', 'bin'):
        links.extend(bin_links(home / root, errors))
    links.extend(cleanup_manifest_links(user, errors))
    return links, errors


def activation_selector(command, args):
    """Return a literal environment selector from common activation and run forms."""
    cmd = posixpath.basename(command)
    if not args:
        return None
    manager = cmd in ('conda', 'mamba', 'micromamba')
    run = manager and args[0] == 'run'
    if not (run or (manager and args[0] == 'activate')
            or (cmd in ('.', 'source') and posixpath.basename(args[0]) == 'activate')):
        return None
    words = iter(args[1:])
    for word in words:
        if word == '--':
            target = None if run else next(words, None)
            return (target, False) if target else None
        if word in ('-n', '--name', '-p', '--prefix'):
            target = next(words, None)
            return (target, word in ('-p', '--prefix')) if target else None
        if word.startswith(('--name=', '--prefix=')):
            return word.split('=', 1)[1], word.startswith('--prefix=')
        if word.startswith(('-n', '-p')) and len(word) > 2:
            return word[2:], word.startswith('-p')
        if word == '--cwd':
            next(words, None)
        if not word.startswith('-'):
            return None if run else (word, False)
    return None


def activation_environments(selector, script, home, environments, names):
    if not selector or not selector[0]:
        return ()
    value, explicit_path = selector
    if not explicit_path and '/' not in value:
        return names.get(value, ())
    for prefix in ('~/', '$HOME/', '${HOME}/'):
        if value.startswith(prefix):
            value = str(home / value[len(prefix):])
            break
    path = posixpath.abspath(posixpath.join(str(script.parent), value))
    if path in environments or toolbox_env_name(path) not in ('', 'r'):
        return (path,)
    return ()


def spark_operation(paths, catalog):
    try:
        names = system_commands()
    except OSError as e:
        return 1, {'error': 'Spark 系统命令名单取不到。', 'detail': e.strerror or str(e)}
    if paths:
        import pwd

        home = Path.home()
        links, issues = toolbox_symlinks(home, pwd.getpwuid(os.geteuid()).pw_name)
        return scan(paths, catalog, names, home=home, links=links, symlink_errors=issues)
    return 0, dict(catalog, system_commands=names)


def scan(paths, catalog, system_names, *, home=None, links=(), symlink_errors=None):
    shadowed = set(system_names)
    commands, envs = defaultdict(list), defaultdict(list)
    for recipe in catalog['recipes']:
        for name in dict.fromkeys(recipe['commands'] + recipe['compat']):
            commands[name].append(recipe)
        envs[recipe['env']].append(recipe)
    links_by_path = {}
    for link in links:
        previous = links_by_path.get(link['path'], {})
        links_by_path[link['path']] = dict(link, environment=link['environment'] or previous.get('environment'))
    link_errors = [] if symlink_errors is None else symlink_errors

    found, errors, seen, skipped = [], [], set(), {}
    skipped_envs, env_sample = set(), []
    # Plain non-script files are only counted: a project tree can hold millions of them.
    other, other_sample = set(), []
    parsers = {'shell': shell_locations, 'python': python_locations,
               'snake': snake_locations, 'nextflow': nextflow_locations}

    def inspect(path):
        try:
            if path.is_symlink():
                skipped.setdefault(os.path.abspath(path), {'file': str(path), 'reason': '软链文件，由调用方核对目标。'})
                return
            identity = path.resolve()
            if identity in seen or identity in skipped or str(identity) in other:
                return
            kind = script_kind(path)
            if kind is None:
                if path.suffix.lower() == '.r':
                    skipped[identity] = {'file': str(path), 'reason': 'R 脚本请人工检查 system() 和 system2()。'}
                else:
                    other.add(str(identity))
                    if len(other_sample) < 20:
                        other_sample.append(str(path))
                return
            seen.add(identity)
            source = path.read_text(encoding='utf-8-sig')
            for command, line, args in sorted(parsers[kind](source), key=lambda item: item[1]):
                if selector := activation_selector(command, args):
                    found.append({'file': str(path), 'line': line, 'command': command, 'selector': selector})
                direct = TOOLBOX_PATH.fullmatch(command)
                name = command.rsplit('/', 1)[-1]
                if name in PUBLIC_R:
                    continue
                if not direct and ('/' in command or command in shadowed or command not in commands):
                    continue
                candidates = (envs.get(direct[2]) if direct and direct[2] else None) or commands.get(name)
                for recipe in candidates or [None]:
                    found.append({'file': str(path), 'line': line, 'command': command,
                                  'tool': recipe['tool'] if recipe else None, 'recipe': recipe})
        except (OSError, SyntaxError, UnicodeError, tokenize.TokenError, RecursionError) as e:
            errors.append({'file': str(path), 'error': '读取或解析失败 (' + type(e).__name__ + ')'})

    def walk_error(error):
        errors.append({'file': error.filename, 'error': '读取目录失败 (' + type(error).__name__ + ')'})

    def skip_directory(directory):
        if directory.is_symlink():
            return True
        if is_environment(directory):
            root = Path(os.path.abspath(directory))
            if root not in skipped_envs:
                skipped_envs.add(root)
                if len(env_sample) < 20:
                    env_sample.append(str(directory))
                if root.name != 'r':
                    for link in bin_links(root / 'bin', link_errors, str(root)):
                        known = links_by_path.setdefault(link['path'], dict(link, source='embedded-env'))
                        if known['environment'] is None:
                            known['environment'] = str(root)
            return True
        return directory.name in GENERATED_DIRS

    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_symlink() or path.is_file():
            inspect(path)
        elif path.is_dir():
            if skip_directory(path):
                continue
            for root, dirs, files in os.walk(path, onerror=walk_error):
                dirs[:] = [d for d in sorted(dirs) if not skip_directory(Path(root) / d)]
                for file in sorted(files):
                    child = Path(root) / file
                    if child.is_symlink() or child.is_file():
                        inspect(child)
        else:
            errors.append({'file': str(path), 'error': '请检查路径是否存在且可读'})

    # Resolve activations after traversal so a script can refer to an environment found later.
    environments, environment_names = defaultdict(list), defaultdict(list)
    for link in links_by_path.values():
        if env := link['environment']:
            if env not in environments:
                environment_names[posixpath.basename(env)].append(env)
            environments[env].append(link)
    lookup_home = home or Path.home()
    hits = []
    for entry in found:
        selector = entry.pop('selector', None)
        if selector is None:
            hits.append(entry)
        elif home is not None or environments:
            for env in activation_environments(selector, Path(entry['file']), lookup_home, environments, environment_names):
                refs = environments.get(env, ())
                target_env = next((toolbox_env_name(link['target']) for link in refs
                                   if link['kind'] == 'environment'), toolbox_env_name(env))
                recipe = next(iter(envs.get(target_env, ())), None)
                hits.append(dict(entry, kind='environment_activation', environment=env,
                                 symlinks=[link['path'] for link in refs],
                                 tool=recipe['tool'] if recipe else None, recipe=recipe))
    output = {'recipe_source': catalog['source'], 'files_scanned': len(seen), 'hits': hits,
              'skipped': list(skipped.values()),
              'skipped_other': {'reason': '脚本格式不在扫描范围内。', 'count': len(other), 'sample': other_sample},
              'skipped_envs': {'count': len(skipped_envs), 'sample': env_sample},
              'symlinks': list(links_by_path.values()), 'symlink_errors': link_errors,
              'errors': errors}
    if 'recipe_errors' in catalog:
        output['recipe_errors'] = catalog['recipe_errors']
    return (1 if errors or link_errors else 0), output


def ws_scan(argv, parser, forward):
    parser.add_argument('paths', nargs='+', metavar='路径')
    parser.add_argument('--recipes', metavar='文件')
    parser.add_argument('--spark', action='store_true', help='在 Spark 上扫描路径及本人家目录的工具箱软链')
    args = parser.parse_args(argv)
    if args.spark:
        if args.recipes:
            parser.error('--recipes 用于 ws 本地扫描。Spark 扫描读取当前面板配方。')
        return forward(['software', 'scan', '--', *args.paths]).returncode
    try:
        path = Path(args.recipes).expanduser() if args.recipes else Path.home() / CATALOG_FILE
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        catalog = recipe_catalog(data)
    except (OSError, ValueError) as e:
        detail = str(e) if isinstance(e, CatalogError) else '读取或解析本地推荐配方文件失败。'
        rc, output = 1, {'error': detail,
                         'next': '先用 software recipes 导出到 ~/' + CATALOG_FILE + '，或用 --recipes 指定配方文件。'}
    else:
        names = data.get('system_commands')
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            rc, output = 1, {'error': '配方文件缺少有效的 Spark 系统命令名单。',
                             'next': '用 software recipes 重新导出配方文件。'}
        else:
            rc, output = scan(args.paths, catalog, names)
            if isinstance(data.get('recipe_errors'), list):
                output.setdefault('recipe_errors', []).extend(data['recipe_errors'])
            output['symlink_note'] = '本地结果列出嵌入环境的软链。Spark 家目录和清理清单请用 software scan --spark。'
    output['ran_on'] = 'ws'
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return rc

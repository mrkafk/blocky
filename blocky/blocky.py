#!/usr/bin/env python2.7

import commands
import contextlib
import sys
import subprocess
import time
import signal
from functools import partial
import daemon
import os
import logging
import logging.handlers
import psutil
from ConfigParser import ConfigParser
from dns import resolver
from dns.resolver import NXDOMAIN
from iptc import Rule, Table
from setproctitle import setproctitle

ips = []

logging.basicConfig()
log = logging.getLogger()
log.setLevel(logging.INFO)

proc_title = 'blocky.py'

whitelist_ipset_name = 'blocky_local_ip_whitelist'

run_foreground = False

# Exceptions

class BlockIPError(Exception):
    pass


class TableNotFound(BlockIPError):
    pass


class ChainNotFound(BlockIPError):
    pass


class IPSetError(BlockIPError):
    pass


class ConfigFileNotFound(Exception):
    pass


class IncorrectCheckEvery(Exception):
    pass


class IncorrectRulePosition(Exception):
    pass

class IncorrectLogType(Exception):
    pass


class IncorrectLogLevel(Exception):
    pass


class IncorrectLogFacility(Exception):
    pass


class LogPathUnset(Exception):
    pass


# Utilities

def flatten(lst):
    flat = []
    for x in lst:
        if hasattr(x, '__iter__') and not isinstance(x, basestring):
            flat.extend(flatten(x))
        else:
            flat.append(x)
    return flat


def parse_comma_separated(s):
    return [x.strip() for x in s.split(',')]


@contextlib.contextmanager
def pidfile_ctxmgr(pidfile_path):
    pid = os.getpid()
    with open(pidfile_path, 'wb') as fo:
        fo.write(str(pid))
    yield
    try:
        os.unlink(pidfile_path)
    except OSError as exc:
        pass


def sigterm_handler_partial(mgr, signum, frame):
    mgr.iptables_handler.delete_rule()
    mgr.ipset_handler.destroy_ipset()
    log.info('Shutdown.')
    sys.exit(0)


def setup_exception_logger(chain=True, log=None):
    import sys
    import traceback
    current_hook = sys.excepthook
    def syslog_exception(etype, evalue, etb):
        if chain:
            current_hook(etype, evalue, etb)
        for line in traceback.format_exception(etype, evalue, etb):
            for line in line.rstrip().split('\n'):
                log.error(line)
    sys.excepthook = syslog_exception


# End Utilities


class LogConfig(object):
    def __init__(self, log_level='info', log_type='syslog', log_facility='daemon', log_path='/var/log/blocky.log'):
        self.log_level = log_level
        self.log_type = log_type
        self.log_facility = log_facility
        self.log_path = log_path

    def set_log_level(self, log_level_name):
        try:
            level = getattr(logging, log_level_name.strip().upper())
            log.setLevel(level)
        except AttributeError:
            raise IncorrectLogLevel(log_level_name)

    def set_handler(self, log_type='syslog', log_facility='daemon', log_path='/var/log/blocky.log', log_level='info'):
        ltype = log_type.lower().strip()
        eff_log_path = log_path.strip()
        if ltype == 'file':
            if not eff_log_path:
                raise LogPathUnset(eff_log_path)
            log.debug('Logging to file %s', log_path)
            self._reset_handlers(log)
            self.set_log_level(log_level)
            fh = logging.FileHandler(log_path)
            self._set_formatter(fh)
            log.addHandler(fh)
            return
        if ltype == 'syslog':
            # we're on Linux anyway
            facility_name = 'LOG_{}'.format(log_facility.strip().upper())
            try:
                log_facility_num = getattr(logging.handlers.SysLogHandler, facility_name)
            except AttributeError:
                raise IncorrectLogFacility(log_type)
            sh = logging.handlers.SysLogHandler(address='/dev/log', facility=log_facility_num)
            log.debug('Logging to syslog handler facility: %s', log_facility)
            self._reset_handlers(log)
            self.set_log_level(log_level)
            log.addHandler(sh)
            self._set_formatter(sh)
            return
        raise IncorrectLogType(log_type)

    def _reset_handlers(self, log):
        for hd in log.handlers:
            log.removeHandler(hd)

    def _set_formatter(self, handler):
        fmt = logging.Formatter('blocky %(levelname)s | %(message)s')
        handler.setFormatter(fmt)


class DetectIPAddresses(object):
    def __init__(self, fqdns=None):
        if fqdns is None:
            fqdns = []
        self.fqdns = fqdns
        self._rslv = resolver.Resolver()

    def _resolve_catch_err(self, fqdn):
        try:
            return self._rslv.query(fqdn, 'A')
        except NXDOMAIN:
            pass
        return []

    def iplist(self):
        log.debug('FQDNs: %s', self.fqdns)
        resolver = self._resolve_catch_err
        addresses = filter(None, flatten([list(resolver(fqdn)) for fqdn in self.fqdns]))
        addresses = list(set([x.address for x in addresses]))
        addresses.sort()
        return addresses


class IPTablesHandler(object):
    def __init__(self, table_name='FILTER', chain_name='FORWARD', ipset_name='blocky', match_set_flag='src', rule_pos=0,
                 comment='Blocky IPTables Rule', target='DROP'):
        self.chain_name = chain_name
        self.table_name = table_name
        self.ipset_name = ipset_name
        self.target = target
        self.chain = None
        self.table = None
        self.rule = None
        self.rule_pos = rule_pos
        self._comment = comment
        self.match_set_flag = match_set_flag
        self._table_find()
        self._chain_find()
        self._rule_find()

    def _table_find(self):
        try:
            self.table = Table(getattr(Table, self.table_name))
        except AttributeError as e:
            raise TableNotFound(self.table_name)

    def _chain_find(self):
        chains = filter(lambda c: c.name == self.chain_name, self.table.chains)
        try:
            self.chain = chains[0]
        except IndexError:
            raise ChainNotFound(self.chain_name)

    def rules(self):
        if not self.chain:
            self._chain_find()
        return self.chain.rules

    def insert_rule(self):
        if not self.rule:
            rule = Rule()
            rule.protocol = 'tcp'
            rule.target = rule.create_target(self.target)
            match = rule.create_match('comment')
            match.comment = self._comment

            match = rule.create_match('set')
            match.match_set = [self.ipset_name, self.match_set_flag]
            self.rule = rule
            log.info(
                '''Inserting a rule with target %s into chain %s (table %s) for ipset "%s" (with comment "%s", rule position: %s)''',
                self.target, self.chain_name, self.table_name, self.ipset_name, self._comment, self.rule_pos)
            self.chain.insert_rule(rule, position=self.rule_pos)

    def delete_rule(self):
        for rule in self.chain.rules:
            for match in rule.matches:
                if match.comment == self._comment:
                    log.info('Deleting blocky IPTables rule (chain %s)', self.chain.name)
                    self.chain.delete_rule(rule)

    def _rule_find(self):
        for rule in self.rules():
            match_with_comment = filter(lambda m: m.comment == self._comment, rule.matches)
            if match_with_comment:
                self.rule = rule
                return rule


class IPSetHandler(object):
    def __init__(self, ipset_name='blocky_blacklist'):
        self.ipset_name = ipset_name
        self.create_ipset_args = 'create {} hash:ip hashsize 4096'.format(self.ipset_name)
        self.path = os.environ.get('PATH', '/sbin:/bin:/usr/sbin:/usr/bin')
        self.iplist_prev = []

    def _env(self):
        return {'PATH': self.path, 'LC_ALL': 'C'}

    def run_ipset_cmd(self, cmds, msg_on_existing_ipset='', msg_on_creating_ipset=''):
        p = subprocess.Popen(cmds, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._env())
        so, se = p.communicate()
        if p.returncode:
            if not so and se.find('set with the same name already exists') > -1:
                log.info(msg_on_existing_ipset)
                return
            raise IPSetError(se)
        if msg_on_creating_ipset:
            log.info(msg_on_creating_ipset)

    def create_ipset(self):
        cmds = flatten(['ipset', self.create_ipset_args.split()])
        log.debug('Creating ipset: %s', cmds)
        self.run_ipset_cmd(cmds, msg_on_existing_ipset='ipset {} exists'.format(self.ipset_name),
                           msg_on_creating_ipset='Creating ipset {}'.format(self.ipset_name))

    def destroy_ipset(self):
        cmds = ['ipset', 'destroy', self.ipset_name]
        log.info('Destroying ipset: %s', self.ipset_name)
        self.run_ipset_cmd(cmds)

    def update_ipset(self, iplist):
        iplist.sort()
        if iplist != self.iplist_prev:
            log.info('Updating ipset %s with IP addresses: %s', self.ipset_name, ', '.join(map(str, iplist)))
            cmds = flatten(['ipset', 'flush', self.ipset_name])
            self.run_ipset_cmd(cmds)
            log.debug(cmds)
            for ip in iplist:
                cmds = flatten(['ipset', 'add', self.ipset_name, str(ip)])
                log.debug(cmds)
                self.run_ipset_cmd(cmds)
            self.iplist_prev = iplist


class Settings(dict):
    def __init__(self, config_file='/etc/blocky.conf',
                 mandatory_fields=['table', 'chain', 'check_every', 'domains', 'ipset', 'log_level', 'log_type',
                                   'pidfile'], **kwargs):
        super(Settings, self).__init__(**kwargs)
        self._config_file = config_file
        self._list_keys = ['domains']
        self._mandatory_fields = mandatory_fields
        self._parse_config()

    def _parse_config(self):
        cp = ConfigParser()
        cp.read(self._config_file)
        if not 'main' in cp.sections():
            raise ConfigFileNotFound(self._config_file)
        visited = set()
        for opt in cp.options('main'):
            val = cp.get('main', opt).strip()
            if val.startswith('@'):
                val = self.check_opt_path(val)
            elif opt in self._list_keys:
                val = [x.strip() for x in val.split(',')]
            # setattr(self, opt, val)
            val = self.check_opt_path(val)
            self[opt] = val
            visited.add(opt)
        diff = set(self._mandatory_fields) - visited
        if diff:
            log.error('Following mandatory option(s) are not set in config file %s: %s. Abort.', self._config_file,
                      ', '.join(map(str, list(diff))))
            sys.exit(1)

    def check_opt_path(self, val):
        if isinstance(val, basestring):
            val = val.strip()
            fpath = None
            if val:
                fpath = val[1:].strip()
            if val.startswith('@') and fpath and os.path.isfile(fpath):
                log.info('Reading values from file %s', fpath)
                with open(fpath, 'rb') as fo:
                    values = [x.strip() for x in fo.readlines()]
                    values = [x for x in values if x and (not x.startswith('#'))]
                    return values
        return val


class StartupChecks(object):
    def __init__(self, settings):
        self.table_name = settings['table']
        self.chain_name = settings['chain']
        self.settings = settings
        self.th = None
        self.rule_pos = None

    def test_prereqs(self):
        self.check_int_check_every()
        self.check_root()
        self.check_command_availability()
        self.check_table_and_chain()
        self.check_pidfile_process()
        self.check_rule_pos_setting()
        self.check_rule_pos()

    def check_command_availability(self):
        for cmd, args in [('iptables', '-L -n'), ('ipset', '-L -n')]:
            status, err = commands.getstatusoutput('{} {}'.format(cmd, args))
            if status:
                print >> sys.stderr, 'ERROR command {} is missing or otherwise unavailable, exit status: {}, error: {}'.format(
                    cmd, status, err)
                sys.exit(status)

    def check_root(self):
        if os.geteuid():
            print >> sys.stderr, 'This program has to be ran by root. Abort.'
            sys.exit(1)

    def check_table_and_chain(self):
        self.th = IPTablesHandler(table_name=self.table_name, chain_name=self.chain_name)
        self.th._chain_find()

    def check_int_check_every(self):
        cev = self.settings.get('check_every')
        try:
            cev = int(cev)
        except ValueError:
            raise IncorrectCheckEvery(cev)
        if cev <= 0:
            raise IncorrectCheckEvery(cev)
        self.settings['check_every'] = cev

    def check_rule_pos_setting(self):
        rpos = self.settings.get('rule_pos', 0)
        msg = 'Incorrect rule position (rule_pos setting, set currently to: {}). Abort.'.format(rpos)
        try:
            rpos = int(rpos)
        except ValueError:
            raise IncorrectRulePosition(msg)
        if rpos < 0:
            raise IncorrectRulePosition(msg)
        self.settings['rule_pos'] = rpos
        self.rule_pos = rpos

    def check_pidfile_process(self):
        pidfile = self.settings['pidfile']
        log.debug('pidfile: %s', pidfile)
        if os.path.isfile(pidfile):
            pid = None
            for line in open(pidfile, 'r'):
                pid = line.strip()
                if pid:
                    break
            try:
                pid = int(pid)
                log.debug('PID %s', pid)
                pid_exists = psutil.pid_exists(pid)
                proc = psutil.Process(pid)
                if pid_exists and proc.name() == 'blocky.py':
                    log.warn('blocky appears to run in process %s. Killing it.', pid)
                    os.kill(pid, signal.SIGTERM)
                    return
                if pid_exists:
                    log.error(
                        'blocky appears to run in process %s, but its name (%s) is different than expected "%s". Abort.',
                        pid, proc.name(), proc_title)
                    sys.exit(10)
            except ValueError:
                return

    def check_rule_pos(self):
        rules = self.th.rules()
        if self.rule_pos > len(rules):
            raise IncorrectRulePosition('Rule position ({}) is too high in IPTables chain (no of rules: {}). Abort.'.format(self.rule_pos, len(rules)))



class BlockManager(object):
    def __init__(self, settings):
        self.settings = settings
        self.iptables_handler = None
        self.ipset_handler = None

    def run(self):
        init_rule_pos = int(self.settings.get('rule_pos', 0))
        # Local IP Whitelist ipset
        self.local_whitelist_ipset_handler = IPSetHandler(ipset_name=whitelist_ipset_name)
        self.local_whitelist_ipset_handler.create_ipset()
        # Local IP Whitelist iptables rule
        self.local_whitelist_iptables_handler = IPTablesHandler(table_name=self.settings['table'],
                                                chain_name=self.settings['chain'],
                                                ipset_name=whitelist_ipset_name,
                                                match_set_flag='dst',
                                                rule_pos=init_rule_pos,
                                                comment='Blocky Whitelist IPTables Rule',
                                                target='ACCEPT')
        self.local_whitelist_iptables_handler.insert_rule()
        self.local_whitelist_ipset_handler.update_ipset(iplist=parse_comma_separated(self.settings.get('whitelist_local_ips', '')))
        # Create blocking ipset
        self.ipset_handler = IPSetHandler(ipset_name=self.settings['ipset'])
        self.ipset_handler.create_ipset()
        # Insert blocking iptables rule
        self.iptables_handler = IPTablesHandler(table_name=self.settings['table'],
                                                chain_name=self.settings['chain'],
                                                ipset_name=self.settings['ipset'],
                                                rule_pos=init_rule_pos+1)
        self.iptables_handler.insert_rule()
        delay = self.settings['check_every']
        log.debug('check_every: %s', delay)
        detect = DetectIPAddresses(fqdns=self.settings['domains'])
        setproctitle(proc_title)
        self.log_startup_notice()
        cnt = 1
        while True:
            iplist = detect.iplist()
            if cnt % 10 == 0:
                log.info('Blocked IP addresses: %s', ', '.join(map(str, iplist)))
                cnt = 0
            cnt += 1
            self.ipset_handler.update_ipset(iplist)
            time.sleep(delay)

    def log_startup_notice(self):
        log.info('blocky (Block-YouTube) startup. Settings:')
        keys = self.settings.keys()
        keys.sort()
        log.info('Config file: %s', self.settings._config_file)
        for k in keys:
            val = self.settings.get(k)
            if isinstance(val, list):
                val = ', '.join(map(str, val))


class Main(object):
    def __init__(self):
        try:
            # Parse config file
            settings = Settings()
            self.logconf = LogConfig()
            log_level = settings.get('log_level', 'info')
            self.logconf.set_log_level(log_level)

            # Do startup checks
            sc = StartupChecks(settings)
            sc.test_prereqs()
            self.settings = settings
            self.logconf.set_handler(log_type=settings.get('log_type', 'syslog'),
                                     log_facility=settings.get('log_facility', 'daemon'),
                                     log_path=settings.get('log_path', '/var/log/blocky.log'),
                                     log_level=log_level)
            setup_exception_logger(log=log)
        except ConfigFileNotFound as e:
            log.error('Config file not found or [main] section is missing: %s', e)
            sys.exit(2)
        except TableNotFound as e:
            log.error('Table %s not found', e)
            sys.exit(3)
        except ChainNotFound as e:
            log.error('Chain %s not found in table %s', e, settings.table)
            sys.exit(4)
        except IPSetError as e:
            log.error('ipset problem: %s', e)
            sys.exit(5)
        except IncorrectCheckEvery as e:
            log.error('Incorrect check_every setting (%s) in config file. Abort.', e)
            sys.exit(6)
        except IncorrectLogType as e:
            log.error('Incorrect log_type setting (%s) in config file. Abort.', e)
            sys.exit(7)
        except IncorrectLogLevel as e:
            log.error('Incorrect log_level setting (%s) in config file. Abort.', e)
            sys.exit(8)
        except IncorrectLogFacility as e:
            log.error('Incorrect log_facility setting (%s) in config file. Abort.', e)
            sys.exit(9)
        except LogPathUnset as e:
            log.error('Log type is set to file, but log_path setting (%s) is empty or incorrect. Abort.', e)
            sys.exit(10)
        except IncorrectRulePosition as e:
            log.error(e)
            sys.exit(10)

    def run(self):
        mgr = BlockManager(self.settings)
        sig_map = {signal.SIGTERM: partial(sigterm_handler_partial, mgr)}
        if run_foreground:
            mgr.run()
        else:
            with daemon.DaemonContext(pidfile=pidfile_ctxmgr(self.settings.get('pidfile', '/var/run/blocky.pid')),
                                      signal_map=sig_map):
                mgr.run()


# DONE: shutdown handler
# DONE: startup notif
# DONE: startup check if pid exists and has title 'blocky'
# DONE: delete ipset on shutdown
# DONE: log blocked ips regularly
# DONE: log blocked ips on change
# DONE: detect rule by comment
# DONE: delete rule on shutdown
# DONE: log to system logger or a file
# DONE: add rule at a config-specified position in chain
# DONE: whitelist local IP addresses
# DONE: read domains to block from a file (@file notation)
# TODO: debian packaging

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '-f':
        run_foreground = True
    m = Main()
    m.run()


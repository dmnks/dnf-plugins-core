# needs_restarting.py
# DNF plugin to check for running binaries in a need of restarting.
#
# Copyright (C) 2014 Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#
# the mechanism of scanning smaps for opened files and matching them back to
# packages is heavily inspired by the original needs-restarting.py:
# http://yum.baseurl.org/gitweb?p=yum-utils.git;a=blob;f=needs-restarting.py

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals
from dnfpluginscore import logger, _

import dnf
import dnf.cli
import dnfpluginscore
import functools
import os
import re
import stat


def list_opened_files(uid):
    for (pid, smaps) in list_smaps():
        try:
            if uid is not None and uid != owner_uid(smaps):
                continue
            with open(smaps, 'r') as smaps_file:
                lines = smaps_file.readlines()
        except EnvironmentError:
            logger.warning("Failed to read PID %d's smaps.", pid)
            continue

        for line in lines:
            ofile = smap2opened_file(pid, line)
            if ofile is not None:
                yield ofile


def list_smaps():
    for dir_ in os.listdir('/proc'):
        try:
            pid = int(dir_)
        except ValueError:
            continue
        smaps = '/proc/%d/smaps' % pid
        yield (pid, smaps)


def memoize(func):
    sentinel = object()
    cache = {}
    def wrapper(param):
        val = cache.get(param, sentinel)
        if val is not sentinel:
            return val
        val = func(param)
        cache[param] = val
        return val
    return wrapper


def owner_uid(fname):
    return os.stat(fname)[stat.ST_UID]


def owning_package(sack, fname):
    matches = sack.query().filter(file=fname).run()
    if matches:
        return matches[0]
    return None


def parse_args(args):
    parser = dnfpluginscore.ArgumentParser(NeedsRestartingCommand.aliases[0])
    parser.add_argument('-u', '--useronly', action='store_true',
                        help=_("only consider this user's processes"))
    return parser.parse_args(args)


def print_cmd(pid):
    cmdline = '/proc/%d/cmdline' % pid
    with open(cmdline) as cmdline_file:
        command = cmdline_file.read()
    command = ' '.join(command.split('\000'))
    print('%d : %s' % (pid, command))


def smap2opened_file(pid, line):
    slash = line.find('/')
    if slash < 0:
        return None
    if line.find('00:') >= 0:
        # not a regular file
        return None
    fn = line[slash:].strip()
    suffix_index = fn.rfind(' (deleted)')
    if suffix_index < 0:
        return OpenedFile(pid, fn, False)
    else:
        return OpenedFile(pid, fn[:suffix_index], True)


class OpenedFile(object):
    RE_TRANSACTION_FILE = re.compile('^(.+);[0-9A-Fa-f]{8,}$')

    def __init__(self, pid, name, deleted):
        self.deleted = deleted
        self.name = name
        self.pid = pid

    @property
    def presumed_name(self):
        """Calculate the name of the file pre-transaction.

        In case of a file that got deleted during the transactionm, possibly
        just because of an upgrade to a newer version of the same file, RPM
        renames the old file to the same name with a hexadecimal suffix just
        before delting it.

        """

        if self.deleted:
            match = self.RE_TRANSACTION_FILE.match(self.name)
            if match:
                return match.group(1)
        return self.name


class ProcessStart(object):
    def __init__(self):
        self.boot_time = self.get_boot_time()
        self.sc_clk_tck = self.get_sc_clk_tck()

    @staticmethod
    def get_boot_time():
        with open('/proc/stat') as stat_file:
            for line in stat_file.readlines():
                if not line.startswith('btime '):
                    continue
                return int(line[len('btime '):].strip())

    @staticmethod
    def get_sc_clk_tck():
        return os.sysconf(os.sysconf_names['SC_CLK_TCK'])

    def __call__(self, pid):
        stat_fn = '/proc/%d/stat' % pid
        with open(stat_fn) as stat_file:
            stats = stat_file.read().strip().split()
        ticks_after_boot = int(stats[21])
        secs_after_boot = ticks_after_boot // self.sc_clk_tck
        return self.boot_time + secs_after_boot


class NeedsRestarting(dnf.Plugin):
    name = 'needs-restarting'

    def __init__(self, base, cli):
        super(NeedsRestarting, self).__init__(base, cli)
        if cli is None:
            return
        cli.register_command(NeedsRestartingCommand)


class NeedsRestartingCommand(dnf.cli.Command):
    aliases = ('needs-restarting',)
    summary = _('determine updated binaries that need restarting')
    usage = ''

    def configure(self, _):
        demands = self.cli.demands
        demands.sack_activation = True

    def run(self, args):
        opts = parse_args(args)
        process_start = ProcessStart()
        owning_pkg_fn = functools.partial(owning_package, self.base.sack)
        owning_pkg_fn = memoize(owning_pkg_fn)

        stale_pids = set()
        uid = os.geteuid() if opts.useronly else None
        for ofile in list_opened_files(uid):
            pkg = owning_pkg_fn(ofile.presumed_name)
            if pkg is None:
                continue
            if pkg.installtime > process_start(ofile.pid):
                stale_pids.add(ofile.pid)

        for pid in sorted(stale_pids):
            print_cmd(pid)

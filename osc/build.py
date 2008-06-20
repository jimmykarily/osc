#!/usr/bin/python

# Copyright (C) 2006 Peter Poeml / Novell Inc.  All rights reserved.
# This program is free software; it may be used, copied, modified
# and distributed under the terms of the GNU General Public Licence,
# either version 2, or (at your option) any later version.



import os
import re
import sys
from tempfile import NamedTemporaryFile
from osc.fetch import *
from osc.core import get_buildinfo, store_read_apiurl, store_read_project, store_read_package, meta_exists, quote_plus, get_buildconfig
import osc.conf
import oscerr
try:
    from xml.etree import cElementTree as ET
except ImportError:
    import cElementTree as ET

from conf import config

change_personality = {
            'i686': 'linux32',
            'i586': 'linux32',
            'i386': 'linux32',
            'ppc': 'powerpc32',
            's390': 's390',
        }

can_also_build = { 
             'x86_64': ['i686', 'i586', 'i386'],
             'i686': ['i586'],
             'i386': ['i586'],
             'ppc64': ['ppc'],
             's390x': ['s390'],
            }

# real arch of this machine
hostarch = os.uname()[4]
if hostarch == 'i686': # FIXME
    hostarch = 'i586'


class Buildinfo:
    """represent the contents of a buildinfo file"""

    def __init__(self, filename, apiurl=config['apiurl']):

        try:
            tree = ET.parse(filename)
        except:
            print >>sys.stderr, 'could not parse the buildconfig:'
            print >>sys.stderr, open(filename).read()
            sys.exit(1)

        root = tree.getroot()

        if root.find('error') != None:
            sys.stderr.write('buildinfo is broken... it says:\n')
            error = root.find('error').text
            sys.stderr.write(error + '\n')
            sys.exit(1)

        if apiurl.startswith('https://') or apiurl.startswith('http://'):
            import urlparse
            scheme, netloc, path = urlparse.urlsplit(apiurl)[0:3]
            apisrv = netloc+path
        else:
            print >>sys.stderr, 'invalid protocol for the apiurl: \'%s\'' % apiurl
            sys.exit(1)

        # are we building  .rpm or .deb?
        # need the right suffix for downloading
        # if a package named debhelper is in the dependencies, it must be .deb
        self.pacsuffix = 'rpm'
        for node in root.findall('dep'):
            if node.text == 'debhelper':
                self.pacsuffix = 'deb'
                break

        self.buildarch = root.find('arch').text

        self.deps = []
        for node in root.findall('bdep'):
            p_name = node.get('name')
            p_arch = node.get('arch')
            if not p_arch:
                p_arch = self.buildarch
            p_version = node.get('version')
            p_release = node.get('release')

            if not (p_name and p_arch and p_version and p_release):
                raise oscerr.APIError(
                    "buildinfo for package %s/%s/%s/%s is incomplete" % (p_name, p_arch, p_version, p_release))

            p = Pac(p_name,
                    p_version,
                    p_release,
                    node.get('project'),
                    node.get('repository'),
                    p_arch,
                    node.get('preinstall'),
                    node.get('runscripts'),
                    self.buildarch,       # buildarch is used only for the URL to access the full tree...
                    self.pacsuffix,
                    scheme,
                    apisrv)
            self.deps.append(p)

        self.preinstall_list = [ dep.name for dep in self.deps if dep.preinstall ]
        self.runscripts_list = [ dep.name for dep in self.deps if dep.runscripts ]

    def has_dep(self, name):
        for i in self.deps:
            if i.name == name:
                return True
        return False

    def remove_dep(self, name):
        for i in self.deps:
            if i.name == name:
                self.deps.remove(i)
                return True
        return False


class Pac:
    """represent a package to be downloaded"""
    def __init__(self, name, version, release, project, repository, arch, 
                 preinstall, runscripts, buildarch, pacsuffix, scheme=config['scheme'], apisrv=config['apisrv']):

        self.name = name
        self.version = version
        self.release = release
        self.arch = arch
        self.project = project
        self.repository = repository
        self.preinstall = preinstall
        self.runscripts = runscripts
        self.buildarch = buildarch
        self.pacsuffix = pacsuffix

        # build a map to fill our the URL templates
        self.mp = {}
        self.mp['name'] = self.name
        self.mp['version'] = self.version
        self.mp['release'] = self.release
        self.mp['arch'] = self.arch
        self.mp['project'] = self.project
        self.mp['repository'] = self.repository
        self.mp['preinstall'] = self.preinstall
        self.mp['runscripts'] = self.runscripts
        self.mp['buildarch'] = self.buildarch
        self.mp['pacsuffix'] = self.pacsuffix

        self.mp['scheme'] = scheme
        self.mp['apisrv'] = apisrv

        self.filename = '%(name)s-%(version)s-%(release)s.%(arch)s.%(pacsuffix)s' % self.mp

        self.mp['filename'] = self.filename


    def makeurls(self, cachedir, urllist):

        self.urllist = []

        # build up local URL
        # by using the urlgrabber with local urls, we basically build up a cache.
        # the cache has no validation, since the package servers don't support etags,
        # or if-modified-since, so the caching is simply name-based (on the assumption
        # that the filename is suitable as identifier)
        self.localdir = '%s/%s/%s/%s' % (cachedir, self.project, self.repository, self.arch)
        self.fullfilename=os.path.join(self.localdir, self.filename)
        self.url_local = 'file://%s/' % self.fullfilename

        # first, add the local URL 
        self.urllist.append(self.url_local)

        # remote URLs
        for url in urllist:
            self.urllist.append(url % self.mp)

    def __str__(self):
        return self.name

    def __repr__(self):
        return "%s" % self.name



def get_built_files(pacdir, pactype):
    if pactype == 'rpm':
        b_built = os.popen('find %s -name \*.rpm' \
                    % os.path.join(pacdir, 'RPMS')).read().strip()
        s_built = os.popen('find %s -name \*.rpm' \
                    % os.path.join(pacdir, 'SRPMS')).read().strip()
    else:
        b_built = os.popen('find %s -name \*.deb' \
                    % os.path.join(pacdir, 'DEBS')).read().strip()
        s_built = None
    return s_built, b_built


def get_prefer_pkgs(dirs, wanted_arch):
    # XXX learn how to do the same for Debian packages
    import glob
    paths = []
    for dir in dirs:
        paths += glob.glob(os.path.join(os.path.abspath(dir), '*.rpm'))
    prefer_pkgs = []

    for path in paths:
        if path.endswith('src.rpm'):
            continue
        if path.find('-debuginfo-') > 0:
            continue
        arch, name = os.popen('rpm -qp --nosignature --nodigest --qf "%%{arch} %%{name}\\n" %s' \
                       % path).read().split()
        # instead of this assumption, we should probably rather take the
        # requested arch for this package from buildinfo
        # also, it will ignore i686 packages, how to handle those?
        if arch == wanted_arch or arch == 'noarch':
            prefer_pkgs.append((name, path))

    return dict(prefer_pkgs)


def main(opts, argv):

    repo = argv[0]
    arch = argv[1]
    spec = argv[2]

    buildargs = []
    if not opts.userootforbuild:
        buildargs.append('--norootforbuild')
    if opts.clean:
        buildargs.append('--clean')
    if opts.noinit:
        buildargs.append('--noinit')
    if not opts.no_changelog:
        buildargs.append('--changelog')
    if opts.jobs:
        buildargs.append('--jobs %s' % opts.jobs)
    if opts.baselibs:
        buildargs.append('--baselibs')
    buildargs = ' '.join(buildargs)

    prj = store_read_project(os.curdir)
    pac = store_read_package(os.curdir)
    if opts.local_package:
        pac = '_repository'
    if opts.alternative_project:
        prj = opts.alternative_project
        pac = '_repository'

    if not os.path.exists(spec):
        print >>sys.stderr, 'Error: specfile \'%s\' does not exist.' % spec
        return 1

    if opts.debuginfo:
        # make sure %debug_package is in the spec-file.
        spec_text = open(spec).read()
        if not re.search(r'(?m)^%debug_package', spec_text):
            spec_text = re.sub(r'(?m)^(%prep)', 
                r'# added by osc build -d\n%debug_package\n\n\1', 
                spec_text, 1)
            tmp_spec = NamedTemporaryFile(prefix = spec + '_', dir = '.', suffix = '.spec')
            tmp_spec.write(spec_text)
            tmp_spec.flush()
            spec = tmp_spec.name
            os.chmod(spec, 0644)


    # make it possible to override configuration of the rc file
    for var in ['OSC_PACKAGECACHEDIR', 'OSC_SU_WRAPPER', 'OSC_BUILD_ROOT']: 
        val = os.getenv(var)
        if val:
            if var.startswith('OSC_'): var = var[4:]
            var = var.lower().replace('_', '-')
            if config.has_key(var):
                print 'Overriding config value for %s=\'%s\' with \'%s\'' % (var, config[var], val)
            config[var] = val

    config['build-root'] = config['build-root'] % {'repo': repo, 'arch': arch}

    print 'Getting buildinfo from server'
    bi_file = NamedTemporaryFile(suffix='.xml', prefix='buildinfo.', dir = '/tmp')
    try:
        bi_text = ''.join(get_buildinfo(store_read_apiurl(os.curdir), 
                                        prj,
                                        pac,
                                        repo, 
                                        arch, 
                                        specfile=open(spec).read(), 
                                        addlist=opts.extra_pkgs))
    except urllib2.HTTPError, e:
        if e.code == 404:
        # check what caused the 404
            if meta_exists(metatype='prj', path_args=(quote_plus(prj), ),
                           template_args=None, create_new=False):
                if meta_exists(metatype='pkg', path_args=(quote_plus(prj), quote_plus(pac)),
                               template_args=None, create_new=False) or pac == '_repository':
                    print >>sys.stderr, 'wrong repo/arch?'
                    sys.exit(1)
                else:
                    print >>sys.stderr, 'The package \'%s\' does not exists - please ' \
                                        'rerun with \'--local-package\'' % pac
                    sys.exit(1)
            else:
                print >>sys.stderr, 'The project \'%s\' does not exists - please ' \
                                    'rerun with \'--alternative-project <alternative_project>\'' % prj
                sys.exit(1)
        else:
            raise
    bi_file.write(bi_text)
    bi_file.flush()

    bi = Buildinfo(bi_file.name, store_read_apiurl(os.curdir))

    rpmlist_prefers = []
    if opts.prefer_pkgs:
        print 'Evaluating preferred packages'
        # the resulting dict will also contain packages which are not on the install list
        # but they won't be installed
        prefer_pkgs = get_prefer_pkgs(opts.prefer_pkgs, bi.buildarch)

        for name, path in prefer_pkgs.iteritems():
            if bi.has_dep(name):
                # We remove a preferred package from the buildinfo, so that the
                # fetcher doesn't take care about them.
                # Instead, we put it in a list which is appended to the rpmlist later.
                # At the same time, this will make sure that these packages are
                # not verified.
                bi.remove_dep(name)
                rpmlist_prefers.append((name, path))
                print ' - %s (%s)' % (name, path)
                continue

    print 'Updating cache of required packages'
    fetcher = Fetcher(cachedir = config['packagecachedir'], 
                      urllist = config['urllist'],
                      auth_dict = config['auth_dict'],
                      http_debug = config['http_debug'])

    # now update the package cache
    fetcher.run(bi)

    if bi.pacsuffix == 'rpm':
        """don't know how to verify .deb packages. They are verified on install
        anyway, I assume... verifying package now saves time though, since we don't
        even try to set up the buildroot if it wouldn't work."""

        if opts.no_verify:
            print 'Skipping verification of package signatures'
        else:
            print 'Verifying integrity of cached packages'
            verify_pacs([ i.fullfilename for i in bi.deps ])

    print 'Writing build configuration'

    rpmlist = [ '%s %s\n' % (i.name, i.fullfilename) for i in bi.deps ]
    rpmlist += [ '%s %s\n' % (i[0], i[1]) for i in rpmlist_prefers ]

    rpmlist.append('preinstall: ' + ' '.join(bi.preinstall_list) + '\n')
    rpmlist.append('runscripts: ' + ' '.join(bi.runscripts_list) + '\n')

    rpmlist_file = NamedTemporaryFile(prefix='rpmlist.', dir = '/tmp')
    rpmlist_file.writelines(rpmlist)
    rpmlist_file.flush()
    os.fsync(rpmlist_file)



    print 'Getting buildconfig from server'
    bc_file = NamedTemporaryFile(prefix='buildconfig.', dir = '/tmp')
    bc_file.write(get_buildconfig(store_read_apiurl(os.curdir), prj, pac, repo, arch))
    bc_file.flush()


    print 'Running build'

    cmd = '%s --root=%s --rpmlist=%s --dist=%s %s %s' \
                 % (config['build-cmd'],
                    config['build-root'],
                    rpmlist_file.name, 
                    bc_file.name, 
                    spec, 
                    buildargs)

    if config['su-wrapper'].startswith('su '):
        tmpl = '%s \'%s\''
    else:
        tmpl = '%s %s'
    cmd = tmpl % (config['su-wrapper'], cmd)
        
    # real arch of this machine 
    # vs.
    # arch we are supposed to build for
    if hostarch != bi.buildarch:

        # change personality, if needed
        if bi.buildarch in can_also_build.get(hostarch, []):
            cmd = change_personality[bi.buildarch] + ' ' + cmd
        else:
            print >>sys.stderr, 'Error: hostarch \'%s\' cannot build \'%s\'.' % (hostarch, bi.buildarch)
            return 1

    print cmd
    rc = os.system(cmd)
    if rc: 
        print
        print 'The buildroot was:', config['build-root']
        sys.exit(rc)

    pacdirlink = os.path.join(config['build-root'], '.build.packages')
    if os.path.exists(pacdirlink):
        pacdirlink = os.readlink(pacdirlink)
        pacdir = os.path.join(config['build-root'], pacdirlink)

        if os.path.exists(pacdir):
            (s_built, b_built) = get_built_files(pacdir, bi.pacsuffix)

            print
            if s_built: print s_built
            print
            print b_built

            if opts.keep_pkgs:
                for i in b_built.splitlines():
                    import shutil
                    shutil.copy2(i, os.path.join(opts.keep_pkgs, os.path.basename(i)))



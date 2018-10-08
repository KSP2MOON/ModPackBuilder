#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""build.py - A modpack generator for the KSP2MOON project

This application generates a modpack based on a recipe provided in YAML
format. It allows for the execution of custom (but documented) code and
also the installation of KSP CKAN modules based on the official specification
published here: https://github.com/KSP-CKAN/CKAN/blob/master/Spec.md. All of
the steps required to build the modpack are documented / captured here.
"""


__author__    = "Marc Vieira-Cardinal"
__copyright__ = "Copyright (C) 2018 The KSP2MOON Project"
__license__   = "GPL-3.0"
__version__   = "1.0"


import os
import re
import git
import yaml
import json
import zipfile
import inspect
import logging
import tarfile
import hashlib
import requests
import argparse
import enlighten
from collections import namedtuple
from pkg_resources import parse_version
from distutils.version import LooseVersion, StrictVersion


def EnsureFolder(location):
    logging.info("Ensuring that folder {} exists".format(location))
    if not os.path.isdir(location):
        os.makedirs(location)


def LoadPackageDefinition(filename):
    if not os.path.isfile(filename):
        raise Exception('The specified package file {} could not be found'.format(filename))

    return yaml.load(open(filename, 'r'))


class CKAN:
    def __init__(self, cacheLocation, ckanMetaURL):
        self.cacheLocation = cacheLocation
        self.ckanDataFilename = os.path.join(self.cacheLocation, "ckan-data.tar.gz")
        self.ckanRegistryFilename = os.path.join(self.cacheLocation, "ckan-registry.json")
        self.ckanMetaURL = ckanMetaURL
        self.registry = {}
        self.logging = logging.getLogger(self.__class__.__name__)

        if not self.ckanMetaURL.endswith(".tar.gz"):
            raise Exception("Expecting MetaURL to end with .tar.gz")

        self.LoadRegistry()

    def SaveRegistry(self):
        self.logging.info("Saving registry to {}".format(self.ckanRegistryFilename))
        open(self.ckanRegistryFilename,'w').write(json.dumps(self.registry,indent=4))

    def LoadRegistry(self):
        if os.path.isfile(self.ckanRegistryFilename):
            self.logging.info("Loading registry from {}".format(self.ckanRegistryFilename))
            self.registry = json.loads(str(open(self.ckanRegistryFilename).read()),strict=False)
        else:
            self.logging.info("Initializing empty registry")
            self.registry = {}

    def RefreshCache(self):
        self.RefreshDataFile()
        self.RefreshRegistryFile()
        self.SaveRegistry()

    def RefreshDataFile(self):
        self.logging.info("Downloading data file {} to {}".format(self.ckanMetaURL, self.ckanDataFilename))
        r = requests.get(self.ckanMetaURL, stream = True)
        fileSize = r.headers.get('content-length')

        with open(self.ckanDataFilename, 'wb') as f:
            if fileSize is None:
                f.write(r.content)
            else:
                manager = enlighten.get_manager()
                progress = manager.counter(total = int(int(fileSize) / 1024), desc = "Downloading:", unit = "kb")

                for data in r.iter_content(chunk_size = 1024):
                    f.write(data)
                    progress.update()

                progress.close()
                manager.stop()

    def RefreshRegistryFile(self):
        self.logging.info("Refreshing registry file {} from {}".format(self.ckanRegistryFilename, self.ckanDataFilename))

        self.registry = {}

        manager = enlighten.get_manager()
        progress = manager.counter(desc = "Processing:", unit = "files")

        if not tarfile.is_tarfile(self.ckanDataFilename):
            raise Exception("Expecting data file to be of tar format")

        with tarfile.open(self.ckanDataFilename, 'r:*') as tar:        
            for item in tar:

                progress.update()

                self.logging.debug("{} | {}".format(
                    item.name,
                    'file' if item.isreg() else 'directory' if item.isdir() else 'other'
                    ))

                if item.isreg():
                    try:
                        itemData = json.loads(str(tar.extractfile(item).read(), 'utf-8'))
                        if not 'identifier' in itemData:
                            if 'name' in itemData:
                                itemData['identifier'] = itemData['name']
                            else:
                                self.logging.warning("Could not find an identifier or name in {}".format(item.name))
                                continue
                        self.registry[item.name] = itemData
                    except ValueError:
                        self.logging.warning("Could not parse {} as json".format(item.name))
                        continue

        progress.close()
        manager.stop()


def PkgFromConfig(config):
    mapping = {
        "ckan": PkgCKAN,
    }

    return mapping[config['type']](config)


class Pkg:
    def __init__(self, config):
        self.childClass = inspect.stack()[1][0].f_locals["self"].__class__.__name__
        self.logging = logging.getLogger(self.childClass)
        self.cacheLocation = os.path.join(config['__args__'].cacheDir, self.childClass)

    def BuildInstallResult(self, provides, conflicts, depends):
        installed = [ self.config['identifier'] ]
        if provides:
            installed.extend(provides)

        return self.InstallResult(
            [ self.FormatDependency(v) for v in installed ],
            [ self.FormatDependency(v) for v in conflicts ],
            [ self.FormatDependency(v) for v in depends ]
            )

    def InstallResult(self, installed, conflicts, dependencies):
        nt = namedtuple('InstallResult', 'installed, conflicts, dependencies')
        return nt(installed, conflicts, dependencies)

    def FormatDependency(self, dependencyIdentifier):
        return "{}:{}".format(self.config['type'], dependencyIdentifier)


class PkgCKAN(Pkg):
    def __init__(self, config):
        super().__init__(config)
        self.config = config

    def Install(self, dryRun = True, failOnMissingDep = True):
        self.logging.info("Installing {}".format(self.FormatDependency(self.config['identifier'])))

        ckanRegistry = self.config['__env__']['ckan'].registry
        matches = [ v for k, v in ckanRegistry.items() if v['identifier'] == self.config['identifier'] ]
        vSortMatches = sorted(matches, key=lambda k: self.VersionParse(k['version']), reverse = True)
        vSortMatchesFiltered = [ v for v in vSortMatches if self.VersionCompatible(self.config['__env__']['ksp_version_min'], self.config['__env__']['ksp_version_max'], v)]

        self.logging.info("Matches found: {}".format(len(vSortMatchesFiltered)))
        for i, v in enumerate(vSortMatchesFiltered):

            installSteps = v.get('install', None)

            depends    = self.NormalizeRelationsList(v.get('depends'   , []))
            conflicts  = self.NormalizeRelationsList(v.get('conflicts' , []))
            provides   = self.NormalizeRelationsList(v.get('provides'  , []))
            recommends = self.NormalizeRelationsList(v.get('recommends', []))
            suggests   = self.NormalizeRelationsList(v.get('suggests'  , []))

            download     = v['download']
            downloadSize = v.get('download_size'        , None)
            downloadHash = v.get('download_hash'        , None)
            downloadType = v.get('download_content_type', None)

            try:
                self.logging.info("---------------------")
                self.logging.info("Listing for match [{}]".format(i))
                self.logging.info("Identifier: {}".format(v['identifier']))
                self.logging.info("Version: {}".format(v['version']))
                self.logging.info("KSP Version: {} - min: {} - max: {}".format(v.get('ksp_version', None), v.get('ksp_version_min', None), v.get('ksp_version_max', None)))
                self.logging.info("Install: {}".format(installSteps))
                self.logging.info("Download: {}".format(download))
                self.logging.info("Download Size: {}".format(downloadSize))
                self.logging.info("Download Hash: {}".format(downloadHash))
                self.logging.info("Download Type: {}".format(downloadType))

                self.logging.info("Depends: {}".format(depends))
                self.logging.info("Conflicts: {}".format(conflicts))
                self.logging.info("Provides: {}".format(provides))
                self.logging.info("Recommends: {}".format(recommends))
                self.logging.info("Suggests: {}".format(suggests))
            except:
                self.logging.error(v)
                raise

        if len(vSortMatchesFiltered) == 0:
            raise Exception("Could not find a suitable package for installation")

        toInstall = vSortMatchesFiltered[0]

        self.logging.info("---------------------")
        self.logging.info("Selected version {} of {}".format(toInstall['version'], toInstall['identifier']))

        self.logging.info("Looking for missing dependencies")
        for dep in self.NormalizeRelationsList(toInstall.get('depends', [])):
            if not self.FormatDependency(dep) in self.config['__env__']['installed']:
                self.logging.warning("Missing dependency {}".format(self.FormatDependency(dep)))
                if failOnMissingDep:
                    raise Exception("Missing dependency {}".format(self.FormatDependency(dep)))

        self.logging.info("Installing" + " (dryrun mode)" if dryRun else "")
        self.RunInstallSteps(dryRun, self.config['__args__'].cacheDir, self.config['__args__'].buildDir, toInstall)

        return self.BuildInstallResult(provides, conflicts, depends)

    def RunInstallSteps(self, dryRun, cacheRoot, kspFolder, package):
        identifier   = package['identifier']

        installSteps = package.get('install', None)

        download     = package['download']
        downloadSize = package['download_size']
        downloadHash = package.get('download_hash', None)
        downloadType = package.get('download_content_type', None)

        cacheFolder = os.path.join(cacheRoot, "ckan")
        filename = os.path.basename(download)
        fnWithPath = os.path.join(cacheFolder, filename)

        if not os.path.isdir(cacheFolder):
            os.mkdir(cacheFolder)

        if not downloadType:
            if filename.lower().endswith(".zip"):
                downloadType = 'application/zip'
            else:
                raise Exception("Not sure what to do with a downloadType of {}".format(downloadType))
        elif downloadType == 'application/zip':
            pass
        else:
            raise Exception("Not sure what to do with a downloadType of {}".format(downloadType))

        if (not os.path.isfile(fnWithPath)
            or os.path.getsize(fnWithPath) != downloadSize
            or ('sha1'   in downloadHash and downloadHash['sha1'].lower()   !=  hashlib.sha1(open(fnWithPath  , "rb").read()).hexdigest())
            or ('sha256' in downloadHash and downloadHash['sha256'].lower() !=  hashlib.sha256(open(fnWithPath, "rb").read()).hexdigest())
            ):

            self.logging.info("Downloading package file {} to {}".format(download, fnWithPath))
            r = requests.get(download, stream = True)
            fileSize = r.headers.get('content-length')
            fileType = r.headers.get('content-type')

            self.logging.info("Reported size: {} type: {}".format(fileSize, fileType))

            with open(fnWithPath, 'wb') as f:
                if fileSize is None:
                    f.write(r.content)
                else:
                    manager = enlighten.get_manager()
                    progress = manager.counter(total = int(int(fileSize) / 1024), desc = "Downloading:", unit = "kb")

                    for data in r.iter_content(chunk_size = 1024):
                        f.write(data)
                        progress.update()

                    progress.close()
                    manager.stop()

        self.logging.info("File is in cache {}".format(fnWithPath))

        if zipfile.is_zipfile(fnWithPath):
            self.logging.info("File is a valid zip file")
        else:
            raise Exception("File is not a valid zip file")

        # from https://github.com/KSP-CKAN/CKAN/blob/master/Spec.md
        # If no install sections are provided, a CKAN client must find the top-most directory in the archive that matches the module identifier, and install that
        # with a target of GameData.
        if installSteps is None:
            self.logging.warning("No installation steps found, using default")
            installSteps = [{"find": self.config['identifier'], "install_to": "GameData"}]

        for step in installSteps:

            if ".." in step['install_to']:
                raise Exception("Trying to escape folder? Found .. in step {}".format(step))
            if "as" in step or "filter_regexp" in step or "include_only" in step or "include_only_regexp" in step:
                raise Exception("Unsupported step directive found")

        with zipfile.ZipFile(fnWithPath, 'r') as z:

            self.logging.info("Compiling list of files to install")
            files = self.FilesToExtract(z, installSteps)

            self.logging.info("Running installation steps" + " (dryrun mode)" if dryRun else "")
            for f in files:
                self.logging.info("Working on {}".format(f))
                self.ExtractFileToPath(dryRun, z, f.filename, f.target, f.makeDirs)


    def FilesToExtract(self, z, steps):
        files = []
        filesItem = namedtuple('FileItem', 'filename, target, makeDirs')

        for step in steps:
            self.logging.debug("Working on step: {}".format(step))

            # from https://github.com/KSP-CKAN/CKAN/blob/master/Spec.md
            # file: The file or directory root that this directive pertains to. All leading directories are stripped from the start of the
            # filename during install. (Eg: MyMods/KSP/Foo will be installed into GameData/Foo.)
            if not step.get('file'):
                step = self.ConvertStepFindToFile(z, step)
            filePrefix = step.get('file')
            self.logging.debug("Will be looking for file prefix {}".format(filePrefix))

            target, makeDirs = self.ComputeTargetAndMakeDirs(step)
            self.logging.debug("Computed target is {}, makeDirs is {}".format(target, makeDirs))

            for f in z.infolist():

                isFolder = f.is_dir()
                isFile = not isFolder
                filename = self.SanitizePath(f.filename)

                self.logging.debug("{} {}".format("File" if isFile else "Folder", f.filename))

                if 'filter' in step and self.FileMatchesFilter(filename, step['filter']):
                    self.logging.debug("Skipping {} (matches filter)".format(filename))
                    continue

                if not filename.startswith(filePrefix):
                    continue

                if re.search(".ckan$", filename, re.IGNORECASE):
                    continue

                if self.FileMatchesFilter(filename, step.get('filter'), step.get('filter_regexp')):
                    continue

                if step.get('include_only') and not self.FileMatchesFilter(filename, step.get('include_only')):
                    continue

                if step.get('include_only_regexp') and not self.FileMatchesFilter(filename, None, step.get('include_only_regexp')):
                    continue

                files.append(filesItem(f.filename, self.BuildTargetPathAndFilename(filePrefix, filename, target, step.get('as')), makeDirs))

        if len(files) == 0:
            raise Exception("No files found for installation")

        return files

    def BuildTargetPathAndFilename(self, prefix, filename, target, _as):
        leadingPathToRemove = os.path.split(self.SanitizePath(prefix))[0]

        # Does anything need to be done for Tutorial or GameRoot ?
        if leadingPathToRemove == "" and prefix in ["GameData", "Ships"]:
            leadingPathToRemove = prefix

            if _as:
                raise Exception("Parameter 'as' is not allowed when 'file' is GameData or Ships")

        if leadingPathToRemove:
            filename = self.RemovePrefix(filename, leadingPathToRemove + "/")

        if _as:
            if "\\" in _as or "/" in _as:
                raise Exception("Parameter 'as' is not allowed to include path separators")
            else:
                filename = self.SplitPath(filename)
                filename[0] = _as
                filename = os.path.join(filename)

        return os.path.join(target, filename)

    def ComputeTargetAndMakeDirs(self, step):
        installTo = self.SanitizePath(step['install_to'])

        if installTo == "GameData" or installTo.startswith("GameData/"):
            makeDirs = True

            subDir = self.RemovePrefix(installTo, "GameData")
            subDir = self.RemovePrefix(subDir   , "/")

            target = self.SanitizePath(os.path.join(self.config['__args__'].buildDir, "GameData", subDir))

        elif installTo.startswith("Ships"):
            makeDirs = False

            if installTo in ['Ships', 'Ships/VAB', 'Ships/SPH', 'Ships/@thumbs', 'Ships/@thumbs/VAB', 'Ships/@thumbs/SPH']:
                target = self.SanitizePath(os.path.join(self.config['__args__'].buildDir, installTo))
            else:
                raise Exception("Invalid install_to found {}".format(installTo))

        else:
            if installTo in ['Tutorial', 'Scenarios', 'Missions']:
                makeDirs = True
                target = self.SanitizePath(os.path.join(self.config['__args__'].buildDir, installTo))
            elif installTo in ['GameRoot']:
                makeDirs = False
                target = self.SanitizePath(os.path.join(self.config['__args__'].buildDir, installTo))
            else:
                raise Exception("Invalid install_to found {}".format(installTo))

        return target, makeDirs


    def SanitizePath(self, path):
        return path.replace("\\", "/").strip("/")


    # from https://github.com/KSP-CKAN/CKAN/blob/master/Spec.md
    # find: (v1.4) Locate the top-most directory which exactly matches the name specified. This is particularly useful when
    # distributions have structures which change based upon each release.
    # find_regexp: (v1.10) Locate the top-most directory which matches the specified regular expression. This is particularly useful
    # when distributions have structures which change based upon each release, but find cannot be used because multiple directories or
    # files contain the same name. Directories separators will have been normalised to forward-slashes first, and the trailing slash for
    # each directory removed before the regular expression is run. Use sparingly and with caution, it's very easy to match the wrong thing
    # with a regular expression.
    def ConvertStepFindToFile(self, z, step):
        if step.get('file'):
            return step

        # Match *only* things with our find string as a directory.
        # We can't just look for directories, because some zipfiles
        # don't include entries for directories, but still include entries
        # for the files they contain.
        reFilter = re.compile(r"(?:^|/)" + re.escape(step['find']) + r"$", re.IGNORECASE) if step.get('find') else re.compile(step.get('find_regexp'), re.IGNORECASE)

        self.logging.debug("reFilter is {}".format(reFilter))
        self.logging.debug("Finding shortest match for find:{}, find_regexp:{}".format(step.get('find'), step.get('find_regexp')))

        shortestMatch = ""

        for f in z.infolist():

            isFolder = f.is_dir()
            isFile = not isFolder
            filename = self.SanitizePath(f.filename)

            self.logging.debug("{} {}".format("File" if isFile else "Folder", f.filename))

            fnSplit = self.SplitPath(filename)
            for i in range(len(fnSplit)):
                fn = os.path.join(*fnSplit[0:i+1])
                self.logging.debug("Trying {}[0:{}] ({})".format(fnSplit, i+1, fn))

                # Complexity here is because sometimes folder entries are not included
                # in zip files but we can extrapolate from file path
                if (
                    reFilter.search(fn)
                    and (len(shortestMatch) == 0 or len(fn) < len(shortestMatch))
                    and (
                        isFolder                                                               # match is already marked as a folder
                        or step.get('find_matches_files')                                      # we are OK with matching both files and folders
                        or (not step.get('find_matches_files') and len(fn) < len(filename) ) # no files allowed, make sure fn is shorter than filename
                        )
                    ):
                    shortestMatch = fn

        if shortestMatch == "" and not step.get('optional'):
            raise Exception("No match found for {}".format(step))

        self.logging.debug("Match found {}".format(shortestMatch))

        if 'find' in step:
            del step['find']
        if 'find_regexp' in step:
            del step['find_regexp']

        step['file'] = shortestMatch

        return step

    def FileMatchesFilter(self, filename, filter = None, filterRe = None):
        # from https://github.com/KSP-CKAN/CKAN/blob/master/Spec.md
        # filter : A string, or list of strings, of file parts that should not be installed. These are treated as literal things which
        # must match a file name or directory. Examples of filters may be Thumbs.db, or Source. Filters are considered case-insensitive.
        # filter_regexp : A string, or list of strings, which are treated as case-sensitive C# regular expressions which are matched against
        # the full paths from the installing zip-file. If a file matches the regular expression, it is not installed.

        # Split the filename into a list of lowercased components
        fnSplit = self.SplitPath(filename)
        fnSplit = [ f.lower() for f in fnSplit ]

        # Normalize filter and filterRe to lists
        if filter and type(filter) != list:
            filter = [filter]
        if filterRe and type(filterRe) != list:
            filterRe = [filterRe]

        # Test each part of the filename against the filters
        for fnPart in fnSplit:
            if filter and fnPart in [f.lower() for f in filter]:
                return True
            if filterRe and [ f for f in filterRe if re.search(f, fnPart) ]:
                return True

        return False

    def ExtractFileToPath(self, dryRun, z, filename, destinationFile, makeDirs):
        if dryRun:
            self.logging.info("Would extract file {} to {} (dryrun)".format(filename, destinationFile))
        else:
            self.logging.info("Extracting file {} to {}".format(filename, destinationFile))

            if z.getinfo(filename).is_dir():
                if not makeDirs:
                    self.logging.debug("Skipping {}, not making directories".format(filename))
                    return

                self.logging.debug("Creating {}".format(destinationFile))
                EnsureFolder(os.path.dirname(destinationFile))
                return

            if not os.path.isdir(os.path.dirname(destinationFile)):
                EnsureFolder(os.path.dirname(destinationFile))
            open(destinationFile, 'wb').write(z.open(filename).read())

    def RemovePrefix(self, text, prefix):
        return text[len(prefix):] if text.startswith(prefix) else text

    def SplitPath(self, path):
        allparts = []
        while 1:
            parts = os.path.split(path)
            if parts[0] == path:  # sentinel for absolute paths
                allparts.insert(0, parts[0])
                break
            elif parts[1] == path: # sentinel for relative paths
                allparts.insert(0, parts[1])
                break
            else:
                path = parts[0]
                allparts.insert(0, parts[1])
        return allparts

    def NormalizeRelationsList(self, relations):
        return [ v['name'] if 'name' in v else v for v in relations ]

    def VersionParse(self, version):
        # LooseVersion or StrictVersion
        # from https://stackoverflow.com/questions/11887762/how-do-i-compare-version-numbers-in-python

        version = version.strip().lstrip("v").lstrip("V")

        # Version epoch
        # from https://wiki.python.org/moin/Distutils/VersionComparison
        # and from https://github.com/KSP-CKAN/CKAN/blob/master/Spec.md
        # epoch is a single (generally small) unsigned integer. It may be omitted, in which case zero is assumed.
        # It is provided to allow mistakes in the version numbers of older versions of a package, and also a package's
        # previous version numbering schemes, to be left behind.
        if ":" not in version:
            version = "0:{}".format(version)

        try:
            # return LooseVersion(version)
            return parse_version(version)
        except Exception as e:
            self.logging.error("Failed to parse version from [{}]".format(version))
            raise

    def VersionCompatible(self, kspVersionMin, kspVersionMax, registryItem):
        kspVersionMin = self.VersionParse(kspVersionMin)
        kspVersionMax = self.VersionParse(kspVersionMax)

        rVersion = registryItem.get('ksp_version',     None)
        rMin     = registryItem.get('ksp_version_min', None)
        rMax     = registryItem.get('ksp_version_max', None)

        if rVersion:
            return rVersion == 'any' or (self.VersionParse(rVersion) >= kspVersionMin and self.VersionParse(rVersion) <= kspVersionMax)
        
        if rMin and rMax:
            return kspVersionMin >= self.VersionParse(rMin) and kspVersionMax <= self.VersionParse(rMax)

        if rMin and not rMax:
            return kspVersionMax >= self.VersionParse(rMin)

        if not rMin and rMax:
            return kspVersionMin <= self.VersionParse(rMax)

        # from https://github.com/KSP-CKAN/CKAN/blob/master/Spec.md
        # If no KSP target version is included, a default of "any" is assumed.
        self.logging.info("Could find version tags, assuming 'any'")
        return True


#############
# Main
###

if __name__ == "__main__":
    argParser = argparse.ArgumentParser()

    argParser.add_argument("--packages",
                           dest    = "packages",
                           action  = "store",
                           default = "packages.yaml",
                           help    = "Path to package definition file (default: %(default)s")

    argParser.add_argument("--ckan-meta-url",
                           dest    = "ckanMetaURL",
                           action  = "store",
                           default = "https://github.com/KSP-CKAN/CKAN-meta/archive/master.tar.gz",
                           help    = "Path to the KSP-CKAN/CKAN-meta archive (default: %(default)s")

    argParser.add_argument("--cache-dir",
                           dest    = "cacheDir",
                           action  = "store",
                           default = ".cache",
                           help    = "Location of the local cache directory (default: %(default)s")

    argParser.add_argument("--build-dir",
                           dest    = "buildDir",
                           action  = "store",
                           default = "build",
                           help    = "Location of the resulting data files (default: %(default)s")

    argParser.add_argument("--log-level",
                           dest    = "logLevel",
                           default = 'DEBUG',
                           choices = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                           help    = "Verbosity of the logs (default: %(default)s")

    args = argParser.parse_args()

    logging.basicConfig(
        level=logging.getLevelName(args.logLevel),
        # format=' %(asctime)s - %(levelname)s - %(message)s')
        format="[%(filename)20s:%(lineno)4s - %(funcName)15s() ][%(levelname)5s][%(name)s] %(message)s")

    logging.debug("Running with args: {}".format(args))

    args.cacheDir = os.path.expanduser(args.cacheDir)
    EnsureFolder(args.cacheDir)

    args.buildDir = os.path.expanduser(args.buildDir)
    EnsureFolder(args.buildDir)

    ckan = CKAN(args.cacheDir, args.ckanMetaURL)
    ckan.RefreshCache()

    packages = LoadPackageDefinition(args.packages)

    defaults = {
        "__args__": args,
        "__env__" : {
            "ckan": ckan,
            "ksp_version_min": packages['ksp_version'],
            "ksp_version_max": packages['ksp_version'],
        }
    }

    installedPkgs = []

    for p in packages['packages']:

        defaults['__env__']['installed'] = installedPkgs
        p = {**defaults, **p}

        pkg = PkgFromConfig(p)
        r = pkg.Install(dryRun = True, failOnMissingDep = False)

        print(r)
        installedPkgs.extend(r.installed)

    logging.info("***************************")
    logging.info("*** I N S T A L L I N G ***")
    logging.info("***************************")

    for p in packages['packages']:

        defaults['__env__']['installed'] = installedPkgs
        p = {**defaults, **p}

        pkg = PkgFromConfig(p)
        r = pkg.Install(dryRun = False, failOnMissingDep = True)

    print(installedPkgs)

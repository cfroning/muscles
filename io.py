# -*- coding: utf-8 -*-
"""
Created on Mon Nov 10 14:28:07 2014

@author: Parke
"""

from os import path
from astropy.io import fits
import numpy as np
from mypy.my_numpy import mids2edges, block_edges, midpts
from scipy.io import readsav as spreadsav
import rc, utils, db
from astropy.table import Table
from astropy.time import Time
from warnings import warn


def readphotons(star, inst):
    pf = db.findfiles('photons', star, inst, fullpaths=True)
    assert len(pf) == 1
    ph = fits.open(pf[0])
    return ph, ph['events'].data


def readFlareTbl(star, inst, label):
    tblfile = db.findfiles(db.flaredir, star, inst, label, 'flares', fullpaths=True)
    assert len(tblfile) == 1
    tbl = Table.read(tblfile[0])

    w0, w1 = [], []
    i = 0
    while True:
        istr = str(i)
        if 'BANDBEG' + str(i) not in tbl.meta:
            break
        w0.append(tbl.meta['BANDBEG' + istr])
        w1.append(tbl.meta['BANDEND' + istr])
        i += 1
    bands = np.array(zip(w0, w1))

    return Table.read(tblfile[0]), bands


def readpans(star):
    """
    Read in all panspectra for a star and return as a list.
    """
    panfiles = db.allpans(star)
    return sum(map(read, panfiles), [])

def read(specfiles):
    """A catch-all function to read in FITS spectra from all variety of
    instruments and provide standardized output as a list of astropy tables.

    The standardized filename 'w_aaa_bbb_ccccc_..._.fits, where aaa is the
    observatory (mod used for modeled data), bbb is the instrument (or model
    type), and ccccc is the filter/grating (w is
    the spectral band) is used to determine how to parse the FITS file .

    The table has columns 'w0','w1' for the wavelength edges, 'flux', 'error',
    'exptime', 'flags', and 'source'. The 'source' column contains a number, where
    muscles.instruments[source_number] gives the aaa_bbb_ccccc string identifying
    the instrument.

    The star keyword is used to reject any spectra that are known to be bad
    for that star.
    """
    #if a list of files is provided, reach each and stack the spectra in a list
    if hasattr(specfiles, '__iter__'):
        return sum(map(read, specfiles), [])

    specfiles = db.validpath(specfiles)

    readfunc = {'fits' : readfits, 'txt' : readtxt, 'sav' : readsav,
                'csv' : readcsv, 'idlsav' : readsav}
    star = db.parse_star(specfiles)
    i = specfiles[::-1].find('.')
    fmt = specfiles[-i:]
    specs = readfunc[fmt](specfiles)
    try:
        sets = rc.loadsettings(star)
        if 'coadd' not in specfiles and 'custom' not in specfiles:
            for config, i in sets.reject_specs:
                if config in specfiles:
                    specs.pop(i)
    except IOError:
        pass
    return specs

def readstdfits(specfile):
    """Read a fits file that was created by writefits."""
    spectbl = Table.read(specfile, hdu=1)
    spectbl.meta['FILENAME'] = specfile
    spectbl.meta['NAME'] = db.parse_name(specfile)
    try:
        sourcespecs = fits.getdata(specfile, 'sourcespecs')['sourcespecs']
        spectbl.meta['SOURCESPECS'] = sourcespecs
    except KeyError:
        spectbl.meta['SOURCESPECS'] = []

    if 'hst' in specfile:
        spectbl = __trimHSTtbl(spectbl)

    spectbl = utils.conform_spectbl(spectbl)

    return spectbl

def readfits(specfile):
    """Read a fits file into standardized table."""

    observatory = db.parse_observatory(specfile)
    insti = rc.getinsti(specfile)

    spec = fits.open(specfile)
    if any([s in specfile for s in ['coadd', 'custom', 'mod', 'panspec']]):
        return [readstdfits(specfile)]
    elif observatory == 'hst':
        sd, sh = spec[1].data, spec[1].header
        flux, err = sd['flux'], sd['error']
        shape = flux.shape
        iarr = np.ones(shape)*insti
        wmid, flags = sd['wavelength'], sd['dq']
        wedges = np.array([mids2edges(wm, 'left', 'linear-x') for wm in wmid])
        w0, w1 = wedges[:,:-1], wedges[:,1:]
        exptarr, start, end = [np.ones(shape)*sh[s] for s in
                               ['exptime', 'expstart', 'expend']]
        normfac = np.ones(shape)
        datas = np.array([w0,w1,flux,err,exptarr,flags,iarr,normfac,start,end])
        datas = datas.swapaxes(0,1)
        spectbls = [__maketbl(d, specfile) for d in datas]

        #cull off-detector data
        spectbls = [__trimHSTtbl(spectbl) for spectbl in spectbls]

    elif observatory == 'xmm':
        sh = spec[0].header
        dw = 5.0
        colnames = ['Wave', 'CFlux', 'CFlux_err']
        wmid, flux, err = [spec[1].data[s] for s in colnames]
        #TODO: make sure to look this over regularly, as these files are likely
        #to grow and change
        wepos, weneg = (wmid[:-1] + dw / 2.0), (wmid[1:] - dw / 2.0)
        if any(abs(wepos - weneg) > 0.01):
            raise ValueError('There are significant gaps in the XMM spectrum'
                             '\n{}'.format(path.basename(specfile)))
        # to ensure gaps aren't introduced due to slight errors...
        we = (wepos + weneg) / 2.0
        w0 = np.insert(we, 0, wmid[0] - dw)
        w1 = np.append(we, wmid[-1] + dw)

        optel = db.parse_spectrograph(specfile)
        if optel == 'pn-':
            expt = sh['spec_exptime_pn']
            start = Time(sh['pn_date-obs']).mjd
            end = Time(sh['pn_date-end']).mjd
        if optel == 'mos':
            expt = (sh['spec_exptime_mos1'] + sh['spec_exptime_mos2']) / 2.0
            start1 = Time(sh['mos1_date-obs']).mjd
            start2 = Time(sh['mos2_date-obs']).mjd
            end1 = Time(sh['mos1_date-end']).mjd
            end2 = Time(sh['mos2_date-end']).mjd
            start = min([start1, start2])
            end = max([end1, end2])

        star = db.parse_star(specfile)
        spectbls = [utils.vecs2spectbl(w0, w1, flux, err, expt,
                                       instrument=insti, start=start, end=end,
                                       star=star, filename=specfile)]
    else:
        raise Exception('fits2tbl cannot parse data from the {} observatory.'.format(observatory))

    spec.close()

    return spectbls

def readtxt(specfile):
    """
    Reads data from text files into standardized astropy table output.
    """

    if 'young' in specfile.lower():
        data = np.loadtxt(specfile)
        wmid, f, e = data.T
        we = mids2edges(wmid)
        w0, w1 = we[:-1], we[1:]
        inst = rc.getinsti(specfile)
        spectbl = utils.vecs2spectbl(w0, w1, f, e, instrument=inst,
                                     filename=specfile)
        return [spectbl]
    else:
        raise Exception('A parser for {} files has not been implemented.'.format(specfile[2:9]))

def readcsv(specfile):
    if db.parse_observatory(specfile) in ['tmd', 'src']:
        data = np.loadtxt(specfile, skiprows=1, delimiter=',')
        wmid, f = data[:,1], data[:,2]
        f *= 100.0 # convert W/m2/nm to erg/s/cm2/AA
        wmid *= 10.0 # convert nm to AA
        we = np.zeros(len(wmid) + 1)
        we[1:-1] = midpts(wmid)
        dw0, dw1 = wmid[1] - wmid[0], wmid[-1] - wmid[-2]
        we[0], we[-1] = wmid[0] - dw0 / 2.0, wmid[-1] + dw1 / 2.0
        w0, w1 = we[:-1], we[1:]
        good = ~np.isnan(f)
        w0, w1, f = w0[good], w1[good], f[good]
        spectbl = utils.vecs2spectbl(w0, w1, f, filename=specfile)
        return [spectbl]
    else:
        raise Exception('A parser for {} files has not been implemented.'.format(specfile[2:9]))

def readsav(specfile):
    """
    Reads data from IDL sav files into standardized astropy table output.
    """
    sav = spreadsav(specfile)
    if 'mod_lya' in specfile:
        wmid = sav['w140']
        flux = sav['lya_mod']
    elif 'sun' in specfile:
        wmid = sav['wave'].squeeze() * 10 # nm to AA
        flux = sav['flux'].squeeze() * 100 # W m-2 nm-2
    we = mids2edges(wmid, 'left', 'linear-x')
    w0, w1 = we[:-1], we[1:]
    N = len(flux)
    err = np.zeros(N)
    expt,flags = np.zeros(N), np.zeros(N, 'i1')
    source = rc.getinsti(specfile)*np.ones(N)
    normfac = np.ones(N)
    start, end = [np.zeros(N)]*2
    data = [w0,w1,flux,err,expt,flags,source,normfac,start,end]
    return [__maketbl(data, specfile)]

def writefits(spectbl, name, overwrite=False):
    """
    Writes spectbls to a standardized MUSCLES FITS file format.
    (Or, rather, this function defines the standard.)

    Parameters
    ----------
    spectbl : astropy table in MUSCLES format
        The spectrum to be written to a MSUCLES FITS file.
    name : str
        filename for the FITS output
    overwrite : {True|False}
        whether to overwrite if output file already exists

    Returns
    -------
    None
    """
    spectbl = Table(spectbl, copy=True)

    # astropy write function doesn't store list meta correctly, so extract here
    # to add later
    sourcespecs = spectbl.meta['SOURCESPECS']
    del spectbl.meta['SOURCESPECS'] #otherwise this makes a giant nasty header
    comments = spectbl.meta['COMMENT']
    del spectbl.meta['COMMENT']

    spectbl.meta['FILENAME'] = name

    #use the native astropy function to write to fits
    spectbl.write(name, overwrite=overwrite, format='fits')

    #but open it up to do some modification
    with fits.open(name, mode='update') as ftbl:

        #add name to first table
        ftbl[1].name = 'spectrum'

        #add column descriptions
        for i,name in enumerate(spectbl.colnames):
            key = 'TDESC' + str(i+1)
            ftbl[1].header[key] = spectbl[name].description

        # add comments
        if len(comments) == 0: comments = ['']
        for comment in comments: ftbl[1].header['COMMENT'] = comment

        #add an extra bintable for the instrument identifiers
        cols = [fits.Column('instruments','13A', array=rc.instruments),
                fits.Column('bitvalues', 'I', array=rc.instvals)]
        hdr = fits.Header()
        hdr['comment'] = ('This extension is a legend for the integer '
                          'identifiers in the instrument '
                          'column of the previous extension. Instruments '
                          'are identified by bitwise flags so that they '
                          'any combination of instruments contributing to '
                          'the data wihtin a spectral element can be '
                          'identified together. For example, if instruments '
                          '4 and 16, 100 and 10000 in binary, both contribute '
                          'to the data in a bin, then that bin will have the '
                          'value 20, or 10100 in binary, to signify that '
                          'both instruments 4 and 16 have contributed. '
                          'This is identical to the handling of bitwise '
                          'data quality flags.')
        idhdu = fits.BinTableHDU.from_columns(cols, header=hdr, name='legend')
        ftbl.append(idhdu)

        #add another bintable for the sourcespecs, if needed
        if len(sourcespecs):
            maxlen = max([len(ss) for ss in sourcespecs])
            dtype = '{:d}A'.format(maxlen)
            col = [fits.Column('sourcespecs', dtype, array=sourcespecs)]
            hdr = fits.Header()
            hdr['comment'] = ('This extension contains a list of the source '
                              'files that were incorporated into this '
                              'spectrum.')
            sfhdu = fits.BinTableHDU.from_columns(col, header=hdr,
                                                  name='sourcespecs')
            ftbl.append(sfhdu)

        ftbl.flush()

def phxdata(Teff, logg=4.5, FeH=0.0, aM=0.0, repo='ftp', ftpbackup=True):
    """
    Get a phoenix spectral data from the repo and return as an array.

    If ftpbackup is True, the ftp repository will be quieried if the file
    isn't found in the specified repo location and the file will be saved
    in the specified location.
    """
    path = rc.phxurl(Teff, logg, FeH, aM, repo=repo)
    try:
        fspec = fits.open(path)
    except IOError:
        if ftpbackup and repo != 'ftp':
            warn('PHX file not found in specified repo, pulling from ftp.')
            rc.fetchphxfile(Teff, logg, FeH, aM, repo=repo)
            fspec = fits.open(path)
        else:
            raise IOError('File not found at {}.'.format(path))
    return fspec[0].data

def __maketbl(data, specfile, sourcespecs=[]):
    star = specfile.split('_')[4]
    return utils.list2spectbl(data, star, specfile, sourcespecs=sourcespecs)

def __trimHSTtbl(spectbl):
    """trim off-detector portions on either end of spectbl"""
    name = path.basename(spectbl.meta['FILENAME'])
    if '_cos_' in name:
        bad = (spectbl['flags'] & 128) > 0
    elif '_sts_' in name:
        bad = (spectbl['flags'] & (128 | 4)) > 0
    beg,end = block_edges(bad)
    if len(beg) >= 2:
        return spectbl[end[0]:beg[-1]]
    elif len(beg) == 1:
        return spectbl[~bad]
    else:
        return spectbl

def write_simple_ascii(spectbl, name, key='flux', overwrite=False):
    """
    Write wavelength and a single spectbl column to an ascii file.
    """

    wmid = (spectbl['w0'] + spectbl['w1']) / 2.0
    f = spectbl[key]
    data = np.array([wmid, f]).T
    np.savetxt(name, data)

def writeMAST(spectbl, name, overwrite=False):
    """
    Writes spectbls to a standardized MUSCLES FITS file format that also
    includes all the keywords required for the archive.

    Parameters
    ----------
    spectbl : astropy table in MUSCLES format
        The spectrum to be written to a MSUCLES FITS file.
    name : str
        filename for the FITS output
    overwrite : {True|False}
        whether to overwrite if output file already exists

    Returns
    -------
    None
    """

    writefits(spectbl, name, overwrite=overwrite)

    with fits.open(name) as hdus:
        h0, h1, h2 = hdus
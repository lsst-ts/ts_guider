# Installing ts_guider on a bare-metal AlmaLinux 9 box

## 1. System packages

Already installed on some machines — check with `rpm -q` first.

```bash
sudo dnf install -y gcc-c++ make cmake git bzip2 \
    readline cfitsio
```

## 2. LSST stack

```bash
mkdir -p ~/lsst_stack && cd ~/lsst_stack
curl -OL https://ls.st/lsstinstall
chmod u+x lsstinstall
./lsstinstall -T w_2026_25
```

If the install finishes with:

```
lsst_stack/conda/envs/lsst-scipipe-13.0.0/share/bash-completion/completions/gdalinfo: line 300: `_gdal-config': not a valid identifier
```

remove the file and re-run:

```bash
rm ~/lsst_stack/conda/envs/lsst-scipipe-13.0.0/share/bash-completion/completions/gdalinfo
./lsstinstall -T w_2026_25 2>&1 | tee lsstinstall.log
```

## 3. Soname shims (SDK built against older libs)

```bash
mkdir ~/readline-shim
ln -sf /usr/lib64/libreadline.so.8 ~/readline-shim/libreadline.so.7
ln -sf /usr/lib64/libbz2.so.1      ~/readline-shim/libbz2.so.1.0
echo $CONDA_PREFIX
ln -sf $CONDA_PREFIX/lib/libcfitsio.so ~/readline-shim/libcfitsio.so.7
```

## 4. `obs_lsst`

```bash
source ~/lsst_stack/loadLSST.sh
eups distrib install -t w_2026_25 obs_lsst
curl -sSL https://raw.githubusercontent.com/lsst/shebangtron/main/shebangtron | python
setup obs_lsst
```

## 5. Clone and build `ts_guider`

```bash
mkdir ts_repos && cd ts_repos
git clone https://github.com/lsst-ts/ts_guider.git
cd ts_guider/
git fetch --all -pP
git checkout tickets/OSW-2409

cd cpp/cmake
cmake -S . -B build
cmake --build build -j

export CCSDAQ=/lsst/daq-sdk/R5-V13.16
export LD_LIBRARY_PATH=~/readline-shim:$CCSDAQ/x86/lib:$LD_LIBRARY_PATH
```

Test it works:

```bash
PYTHONPATH=build python -c "import guiderGDS; print(guiderGDS.__doc__)"
```

## 6. Running (new shell / coming back later)

```bash
source ~/lsst_stack/loadLSST.sh
setup obs_lsst

export CCSDAQ=/lsst/daq-sdk/R5-V13.16
export PATH=$CCSDAQ/x86/bin:$PATH
export LD_LIBRARY_PATH=~/readline-shim:$CCSDAQ/x86/lib:$LD_LIBRARY_PATH

# emulator partition on summit machine is already 'gds-emu'
# DAQ production partition is 'guider'
export GDS_PARTITION='gds-emu'

# partition is already created, no need to run dsid_standalone -p gds-emu
```

### Terminal 1 — pipeline demo

```bash
PYTHONPATH=build:~/ts_repos/ts_guider/python:$PYTHONPATH \
    python run_daq_guider_pipeline_demo.py \
    --partition $GDS_PARTITION --max-stamps 0 --timeout 0 --per-frame
```

### Terminal 2 — emulator

```bash
gds_emulator gds-emu -q 1234 600 /data/ccs-ipa-data/20260702/MC_O_20260702_000024/*guider.fits
```

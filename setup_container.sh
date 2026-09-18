sudo ip link add lsst-daq type dummy
sudo ip addr add 192.168.100.1/24 dev lsst-daq
sudo ip link set lsst-daq up multicast on
sudo ip link set lsst-daq mtu 9000

# Soname shims (SDK built against older libreadline)
mkdir -p ~/readline-shim
ln -sf $CONDA_PREFIX/lib/libreadline.so.8 \
           ~/readline-shim/libreadline.so.7
ln -sf $CONDA_PREFIX/lib/libcfitsio.so  \
           ~/readline-shim/libcfitsio.so.7
ln -sf $CONDA_PREFIX/lib/libbz2.so.1.0 \
           ~/readline-shim/libbz2.so.1.0

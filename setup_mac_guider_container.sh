PLATFORM="linux/amd64"

docker run --platform $PLATFORM --rm --privileged --pid=host alpine \
      nsenter -t 1 -m -u -i -n -p -- \
      sh -c 'sysctl -w net.core.rmem_max=134217728 \
                      net.core.rmem_default=134217728 \
                      net.core.netdev_max_backlog=5000'

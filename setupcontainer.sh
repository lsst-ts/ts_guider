#!/bin/sh
# Do the boring setup for a guider dev container

cd /home/saluser/gitdir/ts_guider
eups declare -r . -t $USER
setup ts_guider -t $USER

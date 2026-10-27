#!/bin/bash

export OP_BUILD_PATH="/home/runner/work/myrouter/myrouter/"

# Clone source code
git clone --single-branch --filter=blob:none -b v22.03.6 https://github.com/openwrt/openwrt openwrt_release
git clone --single-branch --filter=blob:none -b openwrt-22.03 https://github.com/openwrt/openwrt openwrt

# Clone packages
#git clone -b master --depth 1 --filter=blob:none https://github.com/immortalwrt/immortalwrt.git immortalwrt

# Move stable to snapshot
rm -f ./openwrt/include/version.mk
rm -f ./openwrt/include/kernel.mk
rm -f ./openwrt/include/kernel-5.10
rm -f ./openwrt/include/kernel-version.mk
rm -f ./openwrt/include/toolchain-build.mk
rm -f ./openwrt/include/kernel-defaults.mk
rm -f ./openwrt/package/base-files/image-config.in
rm -rf ./openwrt/target/linux/*
rm -rf ./openwrt/package/kernel/linux/*
cp -f ./openwrt_release/include/version.mk ./openwrt/include/version.mk
cp -f ./openwrt_release/include/kernel.mk ./openwrt/include/kernel.mk
cp -f ./openwrt_release/include/kernel-5.10 ./openwrt/include/kernel-5.10
cp -f ./openwrt_release/include/kernel-version.mk ./openwrt/include/kernel-version.mk
cp -f ./openwrt_release/include/toolchain-build.mk ./openwrt/include/toolchain-build.mk
cp -f ./openwrt_release/include/kernel-defaults.mk ./openwrt/include/kernel-defaults.mk
cp -f ./openwrt_release/package/base-files/image-config.in ./openwrt/package/base-files/image-config.in
cp -f ./openwrt_release/version ./openwrt/version
cp -f ./openwrt_release/version.date ./openwrt/version.date
cp -rf ./openwrt_release/target/linux/* ./openwrt/target/linux/
cp -rf ./openwrt_release/package/kernel/linux/* ./openwrt/package/kernel/linux/

shopt -s extglob

cd openwrt/

## Prepare
git clone https://github.com/gSpotx2f/luci-app-cpu-status-mini.git ./package/luci-app-cpu-status-mini

## Update feeds
./scripts/feeds update -a 
patch -p1 < "${OP_BUILD_PATH}"/diy/orangepi-r1/All_openwrt-22.03.6.patch
./scripts/feeds update opicyberwrt
./scripts/feeds update diskman
./scripts/feeds install -a

# Irqbalance
sed -i "s/enabled '0'/enabled '1'/g" feeds/packages/utils/irqbalance/files/irqbalance.config

# My modificaions
sed -i 's/192.168.1.1/192.168.10.1/g' package/base-files/files/bin/config_generate

# Vermagic
wget https://downloads.openwrt.org/releases/22.03.6/targets/ramips/mt7621/packages/Packages.gz
zgrep -m 1 "Depends: kernel (=.*)$" Packages.gz | sed -e 's/.*-\(.*\))/\1/' >.vermagic
sed -i -e 's/^\(.\).*vermagic$/\1cp $(TOPDIR)\/.vermagic $(LINUX_DIR)\/.vermagic/' include/kernel-defaults.mk

exit 0

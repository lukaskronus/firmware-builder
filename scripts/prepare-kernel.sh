#!/bin/sh
# Copy this repository's board DTS, kernel config and thermal driver into a
# histb-mainline/linux checkout, then apply the small Kconfig/Makefile patch.
set -eu

if [ "$#" -ne 1 ]; then
	echo "usage: $0 <kernel-dir>" >&2
	exit 2
fi

KERNEL_DIR=$(CDPATH= cd -- "$1" && pwd)
REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DTS_DIR="$KERNEL_DIR/arch/arm/boot/dts/hisilicon"
DTS_MAKE="$DTS_DIR/Makefile"

install -m 0644 "$REPO_DIR/config_files/hi3798mv100-a312.dts" \
	"$DTS_DIR/hi3798mv100-a312.dts"
install -m 0644 "$REPO_DIR/config_files/hi3798mv100-a312.config" \
	"$KERNEL_DIR/.config"
install -m 0644 "$REPO_DIR/kernel/hi3798mv100_thermal.c" \
	"$KERNEL_DIR/drivers/thermal/hi3798mv100_thermal.c"
install -m 0644 "$REPO_DIR/kernel/hisilicon,hi3798mv100-thermal.yaml" \
	"$KERNEL_DIR/Documentation/devicetree/bindings/thermal/hisilicon,hi3798mv100-thermal.yaml"

if ! grep -q 'hi3798mv100-a312\.dtb' "$DTS_MAKE"; then
	printf '\ndtb-$(CONFIG_ARCH_HISI) += hi3798mv100-a312.dtb\n' >>"$DTS_MAKE"
fi

if ! grep -q '^config HI3798MV100_THERMAL' \
	"$KERNEL_DIR/drivers/thermal/Kconfig"; then
	if git -C "$KERNEL_DIR" rev-parse --git-dir >/dev/null 2>&1; then
		git -C "$KERNEL_DIR" apply "$REPO_DIR/kernel/0001-thermal-kconfig.patch"
	else
		patch -p1 -d "$KERNEL_DIR" <"$REPO_DIR/kernel/0001-thermal-kconfig.patch"
	fi
fi

echo "prepared $KERNEL_DIR"

# Flash new kernel to A312 board

## Backup current kernel

Run this command

```bash
sudo dd if=/dev/mmcblk0p6 of=/root/kernel-backup.bin bs=1M status=progress
```

## Flash new kernel

Use `dd` command to flash to `/dev/mmcblk0p6` parition. Please check your board's partition layout before run `dd` command

```bash
sudo dd if=uImage of=/dev/mmcblk0p6 bs=1M conv=fsync status=progress
```

## Check new kernel

After flash new kernel, create a check file

```bash
size=$(stat -c%s uImage)

sudo dd if=/dev/mmcblk0p6 of=/tmp/kernel-readback.bin bs=1M   count=$(( (size + 1048575) / 1048576 )) status=none
```

Compare the check file with `uImage`

```bash
cmp -n "$size" uImage /tmp/kernel-readback.bin && echo "kernel write OK"
```
Extract modules to `/lib/modules`

```bash
sudo tar -xzvf modules.tar.gz -C /
```

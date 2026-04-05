{pkgs}: {
  deps = [
    pkgs.xorg.libXtst
    pkgs.xorg.libxcb
    pkgs.xorg.libXrandr
    pkgs.xorg.libXfixes
    pkgs.xorg.libXext
    pkgs.xorg.libXdamage
    pkgs.xorg.libXcomposite
    pkgs.xorg.libX11
    pkgs.libxkbcommon
    pkgs.expat
    pkgs.mesa
    pkgs.libdrm
    pkgs.cups
    pkgs.at-spi2-core
    pkgs.at-spi2-atk
    pkgs.gdk-pixbuf
    pkgs.cairo
    pkgs.pango
    pkgs.dbus
    pkgs.alsa-lib
    pkgs.nspr
    pkgs.nss
    pkgs.glib
    pkgs.gtk3
  ];
}

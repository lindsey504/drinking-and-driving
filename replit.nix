{ pkgs }: {
  deps = [
    pkgs.nano
    pkgs.python311
    pkgs.python311Packages.pip
    pkgs.python311Packages.gunicorn
  ];
}

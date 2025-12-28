{
  description = "A very basic flake";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs?ref=nixos-unstable";
  };

  outputs = {
    self,
    nixpkgs,
  }: let
    pkgs = nixpkgs.legacyPackages.x86_64-linux;
    python = pkgs.python3.withPackages (ps: []);
  in {
    devShells.x86_64-linux.default = pkgs.mkShell {
      buildInputs = with pkgs; [
        freecad-wayland
        python
      ];

      shellHook = ''
        export PYTHONPATH="${pkgs.freecad-wayland}/lib:${pkgs.freecad-wayland}/Mod/CAM"
      '';
    };
  };
}

{
  # nix run .          App-Fenster (Argumente nach --, z. B. nix run . -- --debug)
  # nix run .#cli      Kommandozeile: nix run .#cli -- models / serve / login …
  # nix develop        Shell mit allen Abhängigkeiten: python -m edu_ki_proxy app
  description = "EDU-KI Code: Desktop-App für KI-Chats mit Coding-Agent";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      forAllSystems = f: nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ]
        (system: f nixpkgs.legacyPackages.${system});

      pyDeps = ps: with ps; [ fastapi uvicorn websockets httpx playwright wasmtime pygobject3 pypdf ];

      # Typelibs, GIO modules (glib-networking: TLS for WebKit) and GSettings schemas for the window.
      # wrapGAppsHook4 turns them into GI_TYPELIB_PATH, GIO_EXTRA_MODULES and XDG_DATA_DIRS.
      guiLibs = pkgs: with pkgs; [ gtk4 libadwaita webkitgtk_6_0 glib-networking adwaita-icon-theme ];

      app = pkgs:
        let
          module = pkgs.python3.pkgs.toPythonModule (pkgs.runCommand "edu-ki-proxy-module" { } ''
            dst=$out/${pkgs.python3.sitePackages}
            mkdir -p $dst
            cp -r ${./edu_ki_proxy} $dst/edu_ki_proxy
            chmod -R u+w $dst
            ${pkgs.python3.interpreter} -m compileall -q $dst
          '');
          python = pkgs.python3.withPackages (ps: pyDeps ps ++ [ module ]);
        in
        pkgs.stdenvNoCC.mkDerivation {
          pname = "edu-ki-code";
          version = "unstable";
          dontUnpack = true;
          nativeBuildInputs = [ pkgs.wrapGAppsHook4 pkgs.gobject-introspection ];
          buildInputs = guiLibs pkgs;
          dontWrapGApps = true;
          # Chromium for the DeepSeek login and as fallback window; appended to PATH, so one the
          # user already has comes first. -P: never import modules from the current directory.
          preFixup = ''
            wrap() {
              makeWrapper ${python}/bin/python3 $out/bin/$1 "''${gappsWrapperArgs[@]}" \
                --suffix PATH : ${pkgs.lib.makeBinPath [ pkgs.chromium ]} --add-flags "-P -m $2"
            }
            wrap edu-ki-code "edu_ki_proxy app"
            wrap edu-ki-proxy "edu_ki_proxy"
          '';
          meta = {
            description = "EDU-KI Code";
            mainProgram = "edu-ki-code";
            platforms = pkgs.lib.platforms.linux;
          };
        };
    in
    {
      packages = forAllSystems (pkgs: { default = app pkgs; });

      apps = forAllSystems (pkgs: {
        cli = {
          type = "app";
          program = "${app pkgs}/bin/edu-ki-proxy";
          meta.description = "EDU-KI Code: Kommandozeile (serve, login, models, …)";
        };
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          inputsFrom = [ (app pkgs) ];
          packages = [ (pkgs.python3.withPackages pyDeps) pkgs.chromium ];
          # wrapGAppsHook4 only puts these into the wrapper; the shell needs them set directly.
          shellHook = ''
            export XDG_DATA_DIRS="$GSETTINGS_SCHEMAS_PATH:''${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
            export GIO_EXTRA_MODULES="${pkgs.glib-networking}/lib/gio/modules''${GIO_EXTRA_MODULES:+:$GIO_EXTRA_MODULES}"
          '';
        };
      });
    };
}

# Temporary ABI-compatible security grafts; disappear when stable nixpkgs catches up.
{
  pkgs,
  lib,
  hermesAgent,
  cloakbrowser,
  codexCli,
  ...
}:
let
  system = pkgs.stdenv.hostPlatform.system;
  # External applications can retain their own nixpkgs library variants.
  packageSets = [
    pkgs
    hermesAgent.inputs.nixpkgs.legacyPackages.${system}
    cloakbrowser.inputs.nixpkgs.legacyPackages.${system}
    codexCli.inputs.nixpkgs.legacyPackages.${system}
  ];
  deduplicate =
    items:
    builtins.attrValues (
      builtins.listToAttrs (
        map (item: {
          name = builtins.unsafeDiscardStringContext (toString item.old);
          value = item;
        }) items
      )
    );
  libraryFloors = [
    {
      name = "brotli";
      old = pkgs.brotli;
      version = "1.2.1";
      new = pkgs.brotli.overrideAttrs (_: rec {
        version = "1.2.1";
        src = pkgs.fetchFromGitHub {
          owner = "google";
          repo = "brotli";
          tag = "v${version}";
          hash = "sha256-llY72cYffDv0ugsH0p89xIPEbElV4L8u7U/Fe8hT3gI=";
        };
      });
    }
    {
      name = "c-ares";
      old = pkgs.c-ares;
      version = "1.34.9";
      new = pkgs.c-ares.overrideAttrs (_: rec {
        version = "1.34.9";
        src = pkgs.fetchurl {
          url = "https://github.com/c-ares/c-ares/releases/download/v${version}/c-ares-${version}.tar.gz";
          hash = "sha256-k2S3ChLrWaBoMPtIjZNX+QEA3/DZiyPqNZOhg9Q+by8=";
        };
      });
    }
    {
      name = "sqlite";
      old = pkgs.sqlite;
      version = "3.53.4";
      new = pkgs.sqlite.overrideAttrs (old: {
        version = "3.53.4";
        # Bound upstream's separate worker pool and propagate fixture failures.
        env = (old.env or { }) // {
          NJOB = "2";
        };
        # Makefile assigns TSTRNNR_OPTS, so it needs a command-line override.
        checkFlags = (old.checkFlags or [ ]) ++ [ "TSTRNNR_OPTS=--stop-on-error" ];
        src = pkgs.fetchurl {
          url = "https://sqlite.org/2026/sqlite-src-3530400.zip";
          sha256 = "d18fa15aec74d8c17e1463f861095adc01b5ad190256acb4f91d22f0368d232b";
        };
        docsrc = pkgs.fetchurl {
          url = "https://sqlite.org/2026/sqlite-doc-3530400.zip";
          sha256 = "a1d0f5de57485d062796ed7e67daff0758b50d00001a0f233a2c15aaf40bbdc8";
        };
        # The inherited script embeds nixpkgs' original archive version.
        postInstall = ''
          mkdir -p $doc/share/doc
          unzip $docsrc
          mv sqlite-doc-3530400 $doc/share/doc/sqlite
        '';
      });
    }
    {
      name = "openssl";
      old = pkgs.openssl;
      version = "3.6.5";
      new = pkgs.openssl.overrideAttrs (_: rec {
        version = "3.6.5";
        src = pkgs.fetchurl {
          url = "https://github.com/openssl/openssl/releases/download/openssl-${version}/openssl-${version}.tar.gz";
          hash = "sha256-ohV8KDDv3sN4iTmwDJsGODBtPwu7dtxIMu5QO7OX35g=";
        };
      });
    }
  ];
  secureLibrary =
    item:
    let
      base = if lib.versionOlder item.old.version item.version then item.new else item.old;
      sqliteVersions = lib.unique (
        map (packages: packages.sqlite.version) (
          builtins.filter (packages: lib.versionOlder packages.sqlite.version item.version) packageSets
        )
      );
    in
    if item.name == "sqlite" then
      base
      // {
        # SQLite ships no SONAME; keep old versioned filenames for direct loaders.
        out = pkgs.symlinkJoin {
          name = base.out.name;
          paths = [ base.out ];
          postBuild = lib.concatMapStringsSep "\n" (version: ''
            ln -s libsqlite3.so "$out/lib/libsqlite3.so.${version}"
            ln -s libsqlite${base.version}.so "$out/lib/libsqlite${version}.so"
          '') sqliteVersions;
        };
      }
    else
      base;
  candidates = lib.concatMap (
    item:
    map (
      packages:
      item
      // {
        old = packages.${item.name};
        # Reuse the cached base library once it meets the floor, including newer versions.
        new = secureLibrary item;
      }
    ) packageSets
  ) libraryFloors;
  needed = deduplicate (
    builtins.filter (item: lib.versionOlder item.old.version item.version) candidates
  );
  # Replace only runtime outputs. Keep nixpkgs' npm/corepack/dev outputs and
  # cached build consumers; their references to Node are rewritten by the graft.
  nodeCandidates =
    map
      (
        item:
        item
        // {
          new = pkgs.stdenv.mkDerivation (finalAttrs: {
            # Grafting requires equal store-name lengths, including older short versions.
            name = item.old.name;
            version = item.version;
            src = pkgs.fetchurl {
              url = "https://nodejs.org/dist/v${item.version}/node-v${item.version}-linux-x64.tar.xz";
              hash = item.hash;
            };
            nativeBuildInputs = [ pkgs.autoPatchelfHook ];
            buildInputs = [ pkgs.stdenv.cc.cc.lib ];
            strictDeps = true;
            dontConfigure = true;
            dontBuild = true;
            setupHook = item.old.setupHook or null;
            installPhase = ''
              runHook preInstall
              mkdir -p "$out/bin"
              cp -a bin/node "$out/bin/node"
              cp -a include share "$out/"
              runHook postInstall
            '';
            passthru = (item.old.passthru or { }) // {
              tests = (item.old.passthru.tests or { }) // {
                version = pkgs.testers.testVersion {
                  package = finalAttrs.finalPackage;
                  version = "v${item.version}";
                };
              };
            };
            meta = item.old.meta;
          });
        }
      )
      (
        lib.concatMap (
          packages:
          map (item: item // { old = packages.${item.attribute}; }) (
            builtins.filter (item: builtins.hasAttr item.attribute packages) [
              {
                attribute = "nodejs-slim_22";
                version = "22.23.4";
                hash = "sha256-GAW8015oFVWb0uuX20ONP9sOs6XmqXe3wbrcp2M36c4=";
              }
              {
                attribute = "nodejs-slim_24";
                version = "24.22.1";
                hash = "sha256-bBcqmrZxj3uAH7WnvUhq8u5eMnWAhqBLhPzOKqdwleY=";
              }
              {
                attribute = "nodejs-slim_26";
                version = "26.12.1";
                hash = "sha256-YXCy1HThxib0PP863BGN+f47VzJYpPw9hQyNvGsHm70=";
              }
            ]
          )
        ) packageSets
      );
  neededNodes = deduplicate (
    builtins.filter (item: lib.versionOlder item.old.version item.version) nodeCandidates
  );
  replacements =
    lib.concatMap (
      item:
      map (output: {
        oldDependency = item.old.${output};
        newDependency =
          assert lib.assertMsg (builtins.elem output item.new.outputs)
            "Security replacement ${item.new.name} lacks ${item.old.name}'s ${output} output";
          item.new.${output};
      }) item.old.outputs
    ) needed
    ++ map (item: {
      oldDependency = item.old.out;
      newDependency = item.new;
    }) neededNodes;
  check =
    item:
    pkgs.runCommand "${item.name}-security-abi-check"
      {
        nativeBuildInputs = [
          pkgs.binutils
          pkgs.python3
        ];
      }
      ''
        export LC_ALL=C
        count=0
        for original in ${lib.getLib item.old}/lib/lib*.so.*; do
          [ -f "$original" ] && [ ! -L "$original" ] || continue
          soname=$(readelf -d "$original" | sed -n 's/.*Library soname: \[\(.*\)\].*/\1/p')
          loader="$soname"
          if [ -z "$loader" ]; then loader=$(basename "$original"); fi
          replacement=${lib.getLib item.new}/lib/"$loader"
          if [ ! -e "$replacement" ]; then
            echo "Missing replacement for runtime library $original: $replacement" >&2
            exit 1
          fi
          test "$soname" = "$(readelf -d "$replacement" | sed -n 's/.*Library soname: \[\(.*\)\].*/\1/p')"
          for side in original replacement; do
            if [ "$side" = original ]; then target="$original"; else target="$replacement"; fi
            readelf --dyn-syms --wide "$target" \
              | awk '$7 != "UND" && ($5 == "GLOBAL" || $5 == "WEAK") { print $8 }' \
              | sort -u > "$side.exports"
          done
          comm -23 original.exports replacement.exports > missing.exports
          if [ -s missing.exports ]; then
            echo "Removed public symbols from $soname:" >&2
            cat missing.exports >&2
            exit 1
          fi
          count=$((count + 1))
        done
        test "$count" -gt 0
        python ${./scripts/check-runtime-library.py} ${item.name} ${lib.getLib item.new}/lib ${item.new.version}
        touch "$out"
      '';
in
{
  nixpkgs.overlays = [
    (_final: previous: {
      # Nix scans references by hash. Cached ELF files can contain malformed
      # self basenames (e.g. "gtk4k4"), which full-basename rewriting misses.
      replaceDirectDependencies = lib.makeOverridable (
        overrides:
        {
          drv,
          replacements ? [ ],
        }:
        let
          original = (previous.replaceDirectDependencies.override overrides) {
            inherit drv replacements;
          };
          fullNameRule = ''echo "s|$oldBasename|$newBasename|g" >> rewrite.sed'';
          hashRule =
            fullNameRule + "\n" + ''echo "s|''${oldBasename:0:32}|''${newBasename:0:32}|g" >> rewrite.sed'';
        in
        if replacements == [ ] then
          original
        else
          original.overrideAttrs (old: {
            # Retain upstream's complete basename-width check before rewriting.
            buildCommand =
              assert lib.assertMsg (lib.hasInfix fullNameRule old.buildCommand)
                "Security graft helper changed; review hash-based reference rewriting";
              lib.replaceStrings [ fullNameRule ] [ hashRule ] old.buildCommand;
            disallowedReferences =
              (old.disallowedReferences or [ ])
              ++ [ drv ]
              ++ map (replacement: replacement.oldDependency) replacements;
          })
      ) { };
    })
  ];
  assertions = map (pair: {
    assertion =
      builtins.stringLength (baseNameOf (toString pair.oldDependency))
      == builtins.stringLength (baseNameOf (toString pair.newDependency));
    message = "Security graft store names must have equal lengths: ${pair.oldDependency} -> ${pair.newDependency}";
  }) replacements;
  system.replaceDependencies.replacements = replacements;
  system.checks =
    map check needed
    ++ map (
      item:
      pkgs.runCommand "node-${item.version}-security-check" { } ''
        test "$(${item.new}/bin/node --version)" = "v${item.version}"
        ${item.new}/bin/node -e "const z = require('node:zlib'); const input = Buffer.from('runtime security check'); if (!z.brotliDecompressSync(z.brotliCompressSync(input)).equals(input)) process.exit(1)"
        touch "$out"
      ''
    ) neededNodes;
}

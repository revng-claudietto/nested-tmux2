{ pkgs, package }:

pkgs.runCommand "nested-tmux-demo"
  {
    nativeBuildInputs = [
      package
      pkgs.python3
      pkgs.asciinema
      pkgs.asciinema-agg
      pkgs.ffmpeg-headless
      pkgs.tmux
    ];
  }
  ''
    # Do not attach to the terminal or tmux server invoking this build.
    unset TMUX TMUX_PANE TMUX_PARENT
    export HOME="$TMPDIR/home"
    mkdir -p "$HOME" "$out"
    export TERM=xterm-256color
    asciinema rec --headless --return --quiet --output-format asciicast-v2 \
      --window-size 100x28 --title "nested-tmux2: live terminal trees" \
      --command "python3 ${./record.py}" "$out/demo.cast"
    agg --font-dir ${pkgs.dejavu_fonts}/share/fonts/truetype \
      --font-family "DejaVu Sans Mono" --font-size 16 --line-height 1.25 \
      --theme github-dark --fps-cap 15 --last-frame-duration 3 \
      "$out/demo.cast" "$out/demo.gif"
    ffmpeg -hide_banner -loglevel error -i "$out/demo.gif" \
      -vf 'pad=ceil(iw/2)*2:ceil(ih/2)*2' -c:v libx264 -pix_fmt yuv420p \
      -movflags +faststart "$out/demo.mp4"
    test -s "$out/demo.cast"
    test -s "$out/demo.gif"
    test -s "$out/demo.mp4"
  ''

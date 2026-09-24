# Shell integration for zsh: a login shell's profile is the user's own (see .zshenv).
__dsi_enter
if [[ -f ${ZDOTDIR:-$HOME}/.zprofile ]]; then
  source ${ZDOTDIR:-$HOME}/.zprofile
fi
__dsi_leave

"""Monster Hunter web routes."""

from opensnap_web.games.automodellista import AutoModellistaWebModule


class MonsterHunterWebModule(AutoModellistaWebModule):
    """Monster Hunter endpoints currently reuse Auto Modellista legacy web flow."""

    name = 'monsterhunter'

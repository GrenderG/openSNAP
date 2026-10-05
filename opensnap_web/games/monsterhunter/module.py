"""Monster Hunter web routes (NA `mhweb`, EU `mheuweb`, public beta `reweb`)."""

from flask import Blueprint

from opensnap.storage.interfaces import StorageBundle
from opensnap_web.signup import SignupService, add_signup_routes


class MonsterHunterWebModule:
    """Account signup for every Monster Hunter release."""

    name = 'monsterhunter'

    def blueprint(self, storage: StorageBundle) -> Blueprint:
        blueprint = Blueprint(self.name, __name__)
        add_signup_routes(blueprint, SignupService(storage.accounts), ('mhweb', 'mheuweb', 'reweb'))
        return blueprint

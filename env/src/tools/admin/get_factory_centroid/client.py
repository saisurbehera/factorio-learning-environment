from entities import BoundingBox, Position
from instance import PLAYER
from tools.tool import Tool


class GetFactoryCentroid(Tool):
    def __init__(self, lua_script_manager, game_state):
        self.state = { 'input': {}, 'output': {} }
        super().__init__(lua_script_manager, game_state)

    def __call__(self) -> BoundingBox:
        """
        Gets the bounding box of the enti factory.
        """

        result, _ = self.execute(PLAYER)

        if isinstance(result, str):
            raise Exception(result)

        result = self.clean_response(result)

        try:
            return BoundingBox(left_top=Position(x=result['bounds']['left_top']['x'],y=result['bounds']['left_top']['y']),
                               right_bottom=Position(x=result['bounds']['right_bottom']['x'],y=result['bounds']['right_bottom']['y']),
                               left_bottom=Position(x=result['bounds']['left_top']['x'],y=result['bounds']['right_bottom']['y']),
                               right_top=Position(x=result['bounds']['right_bottom']['x'], y=result['bounds']['left_top']['y']))
        except:
            return None


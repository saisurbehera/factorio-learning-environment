import pytest

from entities import Position, Entity
from instance import Direction
from game_types import Prototype, Resource

@pytest.fixture()
def base_game(instance):
    instance.initial_inventory = {'pumpjack': 1,
                                  'pipe': 100,
                                  'burner-inserter': 6,
                                  'coal': 50,
                                  'boiler': 1,
                                  'steam-engine': 1,
                                  'small-electric-pole': 20,
                                  'offshore-pump': 1,
                                  "chemical-plant": 1,
                                  'oil-refinery': 1,
                                  'transport-belt': 50,
                                  'burner-mining-drill': 5}
    instance.reset()
    instance.speed(10)
    instance.add_command('/c local oil_resources = game.surfaces[1].find_entities_filtered{name="crude-oil"}\nfor _, oil in pairs(oil_resources) do\n\toil.destroy()\nend', raw=True)
    instance.execute_transaction()
    instance.add_command('/c game.surfaces[1].create_entity{name="crude-oil", position={x=-10, y=-5}}', raw=True)
    instance.execute_transaction()
    yield instance.namespace

@pytest.fixture()
def game(base_game):
    """Create electricity system"""
    inventory = base_game.inspect_inventory()
    # move to the nearest water source
    water_location = base_game.nearest(Resource.Water)
    base_game.move_to(water_location)

    offshore_pump = base_game.place_entity(Prototype.OffshorePump,
                                      position=water_location)
    # Get offshore pump direction
    direction = offshore_pump.direction

    # place the boiler next to the offshore pump
    boiler = base_game.place_entity_next_to(Prototype.Boiler,
                                       reference_position=offshore_pump.position,
                                       direction=direction,
                                       spacing=2)
    assert boiler.direction.value == direction.value

    # rotate the boiler to face the offshore pump
    boiler = base_game.rotate_entity(boiler, Direction.next_clockwise(direction))

    # insert coal into the boiler
    base_game.insert_item(Prototype.Coal, boiler, quantity=5)

    # connect the boiler and offshore pump with a pipe
    offshore_pump_to_boiler_pipes = base_game.connect_entities(offshore_pump, boiler, connection_type=Prototype.Pipe)

    base_game.move_to(Position(x=0, y=10))
    steam_engine: Entity = base_game.place_entity_next_to(Prototype.SteamEngine,
                                                     reference_position=boiler.position,
                                                     direction=boiler.direction,
                                                     spacing=1)

    base_game.connect_entities(steam_engine, boiler, connection_type=Prototype.Pipe)

    yield base_game


def test_build_chemical_plant(game):
    # Start at the origin
    game.move_to(game.nearest(Resource.CrudeOil))
    pumpjack = game.place_entity(Prototype.PumpJack,
                                 direction=Direction.DOWN,
                                 position=game.nearest(Resource.CrudeOil))

    # Start at the origin
    game.move_to(Position(x=0, y=-6))

    refinery = game.place_entity(Prototype.OilRefinery,
                                 direction=Direction.DOWN,
                                 position=Position(x=0, y=-6))

    # Start at the origin
    game.move_to(Position(x=0, y=0))

    chemical_plant = game.place_entity(Prototype.ChemicalPlant,
                                       direction=Direction.DOWN,
                                       position=Position(x=0, y=0))

    steam_engine = game.get_entity(Prototype.SteamEngine, game.nearest(Prototype.SteamEngine))



    game.connect_entities(pumpjack, refinery, connection_type=Prototype.Pipe)
    game.connect_entities(refinery, chemical_plant, connection_type=Prototype.Pipe)
    game.connect_entities(pumpjack, refinery, chemical_plant, steam_engine, connection_type=Prototype.SmallElectricPole)

    #game.connect_entities(pumpjack, steam_engine, connection_type=Prototype.SmallElectricPole)
    entities = game.get_entities()
    # Find the nearest coal patch
    coal_patch = game.get_resource_patch(Resource.Coal, game.nearest(Resource.Coal))

    # Move to the center of the coal patch
    game.move_to(coal_patch.bounding_box.left_top)


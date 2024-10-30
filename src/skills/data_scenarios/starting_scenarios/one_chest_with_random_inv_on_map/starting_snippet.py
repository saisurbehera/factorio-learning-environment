from factorio_instance import *

NAME_TO_ENTITY_MAPPING = {"coal": Prototype.Coal, "iron-ore": Prototype.IronOre, 
                          "copper-ore": Prototype.CopperOre, "stone": Prototype.Stone, 
                          "iron-plate": Prototype.IronPlate, "copper-plate": Prototype.CopperPlate, 
                          }

furnace_pos = Position(x = -12, y = -12)
move_to(furnace_pos)
stone_furnace = place_entity(Prototype.WoodenChest, Direction.UP, furnace_pos)

inventory = inspect_inventory()
for item in inventory:
    name = item[0]
    count = item[1]
    updated_chest = insert_item(NAME_TO_ENTITY_MAPPING[name], stone_furnace, count)
import unittest

from fasalrin_fresh import block_from_address


class TalukaSpellingTests(unittest.TestCase):
    def test_devgadbaria_address_matches_devgadhbaria_option(self):
        address = "SAGTALA,DEVGADBARIA,DEVGADBARIA,DOHAD, DIST DAHOD"
        self.assertEqual(block_from_address(address, ["Dahod", "Devgadhbaria"]), "Devgadhbaria")


if __name__ == "__main__":
    unittest.main()

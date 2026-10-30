import unittest
from unittest.mock import Mock, patch

import fasalrin_fresh as fresh
from fasalrin_fresh import block_from_address


class TalukaSpellingTests(unittest.TestCase):
    def test_devgadbaria_address_matches_devgadhbaria_option(self):
        address = "SAGTALA,DEVGADBARIA,DEVGADBARIA,DOHAD, DIST DAHOD"
        self.assertEqual(block_from_address(address, ["Dahod", "Devgadhbaria"]), "Devgadhbaria")

    def test_repeated_district_does_not_outvote_named_taluka(self):
        address = "DABHVA SAGTALA DEVGADBARIA DAHOD GUJARAT, DAHOD, GUJARAT"
        self.assertEqual(block_from_address(address, ["Dohad", "Devgadhbaria"]), "Devgadhbaria")

    def test_district_only_address_does_not_claim_taluka(self):
        address = "JUNI BEDI, SAGTALA DAHOD GUJARAT, DAHOD, GUJARAT"
        self.assertIsNone(block_from_address(address, ["Dohad", "Devgadhbaria"]))

    def test_explicit_dahod_taluka_is_respected(self):
        address = "AT BARA TA DAHOD DIST DAHOD"
        self.assertEqual(block_from_address(address, ["Dohad", "Devgadhbaria"]), "Dohad")

    def test_dahod_named_without_district_suffix_can_be_taluka(self):
        self.assertEqual(block_from_address('AT DAHOD', ['Dohad', 'Devgadhbaria']), 'Dohad')

    def test_district_only_address_uses_branch_usual_taluka(self):
        page, scope = Mock(), Mock()
        branch_addresses = {fresh.norm('Dabhva'): [
            'DABHVA SAGTALA DEVGADBARIA DAHOD GUJARAT, DAHOD, GUJARAT',
            'DUDHIYA DEVGADBARIA DAHOD GUJARAT, DAHOD, GUJARAT']}
        with patch.object(fresh, 'BRANCH_ADDRESSES', branch_addresses), \
             patch.object(fresh, 'wait_for'), \
             patch.object(fresh, 'options_of', return_value=['Select', 'Dohad', 'Devgadhbaria']), \
             patch.object(fresh, 'select_label'), \
             patch.object(fresh, 'form_values', return_value={'resSubDistrictId': 'Devgadhbaria'}):
            chosen = fresh.pick_block(page, scope, 'resSubDistrictId', 'Dabhva',
                                      'JUNI BEDI, SAGTALA DAHOD GUJARAT, DAHOD, GUJARAT')
        self.assertEqual(chosen, 'Devgadhbaria')


if __name__ == "__main__":
    unittest.main()

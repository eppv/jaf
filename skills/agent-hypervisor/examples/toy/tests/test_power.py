import unittest

from toy import calc


class PowerTest(unittest.TestCase):
    def test_power(self):
        self.assertEqual(calc.power(2, 10), 1024)


if __name__ == "__main__":
    unittest.main()

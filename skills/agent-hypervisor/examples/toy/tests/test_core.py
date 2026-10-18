import unittest

from toy import calc


class CalcTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(calc.add(2, 3), 5)

    def test_mul(self):
        self.assertEqual(calc.mul(2, 3), 6)


if __name__ == "__main__":
    unittest.main()
